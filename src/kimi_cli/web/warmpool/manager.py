"""WarmPoolManager — keeps N pre-warmed sandbox Pods and hands them to sessions.

# hechun-fork-cci (warm pool; spec 2026-09-07-sandbox-warmpool-design.md)

Why this exists: a cold session costs 30–39s, ~70% of it Pod creation and most
of the rest worker boot. A Pod that is already Running with a worker that has
already imported everything and downloaded its assets can be bound to a real
session in about the time ``KimiCLI.create`` takes (~2.6s) — and that last part
cannot be pre-done, because MCP headers embed ``${KIMI_USER_ID}`` at build time
(spec §4.4).

Design decisions worth not re-deriving:

* **Warm Pods are NOT registered in the runner.** ``runner.iter_sessions()`` is
  keyed by session id, and the gateway idle sweeper walks exactly that mapping.
  Registering a warm Pod under a placeholder id would mean inventing an
  exemption in the sweeper (and in every other iter_sessions consumer) purely to
  undo a registration that buys nothing — the manager owns these Pods and their
  lifecycle. The exemption is therefore *structural*: the sweeper cannot see a
  warm Pod. The moment a Pod is claimed it becomes an ordinary
  ``CCISessionProcess`` in the runner, so normal idle reclaim resumes with no
  special case anywhere (spec §7's "``claimed`` 后立即恢复正常回收").
* **The DB row, not the Pod name, is the mapping.** Warm Pods are named
  ``kimo-sandbox-{uuid}`` with no relation to the session that eventually gets
  them.
* **Every failure path degrades to a cold start.** Empty pool, lost claim race,
  bind rejected, probe timeout — all of them return ``None`` from
  :meth:`acquire` and the caller spawns normally. None of them are errors and
  none should alert.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from kimi_cli import logger
from kimi_cli.web.runner import warm_protocol as wp
from kimi_cli.web.warmpool.store import (
    STATE_CLAIMED,
    STATE_DEAD,
    STATE_READY,
    STATE_WARMING,
    WarmPoolStore,
    new_placeholder_session_id,
    new_pod_name,
)

if TYPE_CHECKING:
    from kimi_cli.web.metrics import MetricsState
    from kimi_cli.web.spawner import SandboxHandle, SandboxSpawner

ENV_ENABLED = "KIMO_WARMPOOL_ENABLED"
ENV_SIZE = "KIMO_WARMPOOL_SIZE"
ENV_AGENT = "KIMO_WARMPOOL_AGENT"
ENV_REFILL_INTERVAL = "KIMO_WARMPOOL_REFILL_INTERVAL_SECONDS"
ENV_READY_TIMEOUT = "KIMO_WARMPOOL_READY_TIMEOUT_SECONDS"
ENV_BIND_TIMEOUT = "KIMO_WARMPOOL_BIND_TIMEOUT_SECONDS"
ENV_PING_TIMEOUT = "KIMO_WARMPOOL_PING_TIMEOUT_SECONDS"
ENV_KEEPALIVE_INTERVAL = "KIMO_WARMPOOL_KEEPALIVE_INTERVAL_SECONDS"

DEFAULT_AGENT = "diabetes-expert"

#: Refill backoff (seconds): doubles per consecutive failure up to the cap. A
#: gateway that cannot reach CCI must not retry a failing spawn every interval
#: forever (spec §8.4).
_BACKOFF_START_S = 30
_BACKOFF_CAP_S = 900

#: Keepalive interval (seconds). The exec stream that carries the warm
#: handshake dies after roughly 5–6 minutes of silence (2026-09-07: the single
#: ``probe_failed`` we saw happened in the 6.5-minute window where the backend
#: had not yet been deployed and nobody was pinging). 60s is well inside that
#: and is the cadence already proven to hold the stream open.
_KEEPALIVE_INTERVAL_S = 60

#: :meth:`WarmPoolManager._warm_one` outcomes. ``SKIPPED`` is not a failure: it
#: means the pool is already at capacity according to the DB (another gateway,
#: or an in-flight row this process cannot see), so it must NOT trip the backoff
#: that exists for a broken CCI.
_WARM_OK = "warmed"
_WARM_FAILED = "failed"
_WARM_SKIPPED = "skipped"


class WarmPod:
    """One warmed Pod the manager still owns (Running, worker awaiting bind)."""

    __slots__ = ("pod_name", "handle", "stream", "agent", "lock", "ready_at")

    def __init__(self, pod_name: str, handle: SandboxHandle, stream: Any, agent: str) -> None:
        self.pod_name = pod_name
        self.handle = handle
        self.stream = stream
        self.agent = agent
        # Serialises stream access: a health probe and a claim must never
        # interleave reads on the same exec stream.
        self.lock = asyncio.Lock()
        self.ready_at = time.monotonic()


class WarmPoolManager:
    """Owns warm Pods + their ``kimo_sandbox_pod`` rows."""

    def __init__(
        self,
        *,
        spawner: SandboxSpawner,
        store: WarmPoolStore,
        env_builder: Callable[[], dict[str, str]],
        size: int = 1,
        agent: str = DEFAULT_AGENT,
        metrics: MetricsState | None = None,
        refill_interval_s: int = 30,
        ready_timeout_s: float = 120.0,
        bind_timeout_s: float = 20.0,
        ping_timeout_s: float = 10.0,
        keepalive_interval_s: int = _KEEPALIVE_INTERVAL_S,
    ) -> None:
        self._spawner = spawner
        self._store = store
        #: callable() -> dict[str, str]: the env a warm Pod is created with.
        #: Deliberately identity-free — a warm Pod is created before any user
        #: exists, and identity arrives in the bind frame.
        self._env_builder = env_builder
        self._size = size
        self._agent = agent
        self._metrics = metrics
        self._refill_interval_s = refill_interval_s
        self._ready_timeout_s = ready_timeout_s
        self._bind_timeout_s = bind_timeout_s
        self._ping_timeout_s = ping_timeout_s
        self._keepalive_interval_s = keepalive_interval_s

        self._pods: dict[str, WarmPod] = {}
        self._lock = asyncio.Lock()
        #: Serialises the whole "decide how many are missing → create them"
        #: critical section. ``_lock`` only guards reads/writes of ``_pods`` and
        #: is therefore useless here: the decision is made *outside* it and a
        #: warm-up takes 20–30s, so two triggers could both read "0 < 1" and
        #: each start a Pod (observed on test, 2026-09-07).
        self._refill_lock = asyncio.Lock()
        self._refill_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._backoff_s = 0
        self._hits = 0
        self._misses = 0

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def enabled_from_env(cls) -> bool:
        return (os.environ.get(ENV_ENABLED) or "").strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def size_from_env(cls) -> int:
        raw = (os.environ.get(ENV_SIZE) or "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else 1

    @classmethod
    def agent_from_env(cls) -> str:
        return (os.environ.get(ENV_AGENT) or DEFAULT_AGENT).strip() or DEFAULT_AGENT

    @classmethod
    def keepalive_interval_from_env(cls) -> int:
        """Seconds between self-keepalive passes; ``0`` disables them."""
        raw = (os.environ.get(ENV_KEEPALIVE_INTERVAL) or "").strip()
        return int(raw) if raw.isdigit() else _KEEPALIVE_INTERVAL_S

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Reconcile stale rows, then start the background refill loop.

        🔴 The reconcile is not optional. ``CCISpawner.reconcile_orphans`` (which
        runs just before this, on every gateway start) deletes every
        ``kimo-sandbox-*`` Pod in the namespace, warm Pods included. Any row left
        saying ``ready`` would then describe a Pod that no longer exists, and the
        next claim would "succeed" against nothing — worse than an empty pool,
        because it bypasses the cold-start fallback (spec §9).
        """
        try:
            names = await asyncio.to_thread(self._store.mark_all_live_dead, "gateway_restart")
            if names:
                logger.info(
                    "[warmpool] gateway restart: marked {n} stale row(s) dead: {names}",
                    n=len(names),
                    names=", ".join(names[:10]),
                )
        except Exception as e:  # noqa: BLE001 — never block gateway startup
            logger.warning("[warmpool] startup reconcile failed (continuing): {err}", err=e)

        self._refill_task = asyncio.create_task(self._refill_loop())
        if self._keepalive_interval_s > 0:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info(
            "[warmpool] started size={n} agent={a} refill_interval={i}s keepalive={k}s",
            n=self._size,
            a=self._agent,
            i=self._refill_interval_s,
            k=self._keepalive_interval_s or "off",
        )

    async def close(self) -> None:
        """Cancel the refill loop and release every Pod still in the pool."""
        for attr in ("_refill_task", "_keepalive_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, attr, None)
        async with self._lock:
            pods = list(self._pods.values())
            self._pods.clear()
        for pod in pods:
            await self._discard(pod, reason="gateway_shutdown")

    async def _refill_loop(self) -> None:
        """Fill the pool now, then keep it topped up.

        The first pass runs immediately rather than after one interval:
        otherwise every gateway restart leaves a window in which the pool is
        empty for no reason and every session in it pays the full cold start.
        """
        while True:
            try:
                await self.refill()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the loop must never die
                logger.warning("[warmpool] refill pass errored (continuing): {err}", err=e)
            # Backoff (set by a failed warm-up) takes precedence over the
            # steady-state interval, so a broken CCI is retried ever more slowly.
            await asyncio.sleep(max(1, self._backoff_s or self._refill_interval_s))

    async def _keepalive_loop(self) -> None:
        """Keep the manager's own Pods alive. **The gateway owns this, not the backend.**

        🔴 Do not delete this in favour of the backend health-check endpoint.
        Until 2026-09-07 the only thing pinging warm Pods was the backend's 60s
        sweep, which made two supposedly independent switches secretly coupled:
        with ``AI_WARMPOOL_ENABLED=false`` — or simply while the backend was
        restarting — the exec stream went silent, died after ~5–6 minutes, the
        Pod was evicted and refilled, and the pool churned CCI Pods for nothing
        with no one watching. Whoever holds a Pod is responsible for keeping it
        alive; the backend sweep is now an *external* check (dirty-row reclaim),
        not the lifeline.
        """
        while True:
            await asyncio.sleep(self._keepalive_interval_s)
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the loop must never die
                logger.warning("[warmpool] keepalive pass errored (continuing): {err}", err=e)

    # ── refill ───────────────────────────────────────────────────────────────

    async def refill(self) -> int:
        """Top the pool back up to ``size``. Returns how many Pods were warmed.

        Refill has four triggers that do not coordinate with each other (startup,
        the 30s loop, every claim, the backend health sweep) and one pass takes
        20–30s. Two of them arriving in the same window used to each start a Pod,
        because the capacity test counted only Pods that had already reported
        ready — the one being created was invisible. Two guards now cover the two
        halves of that: ``_refill_lock`` makes the passes in *this* process
        strictly sequential (so an in-flight Pod is never raced by a sibling
        trigger), and the DB capacity check in :meth:`_warm_one` covers everyone
        else, because the ``warming`` row — written the moment a warm-up starts —
        is exactly the in-flight state that memory cannot see.

        A pass that arrives while another is running returns 0 rather than
        queueing: by the time the running one finishes, the pool is full, and a
        queued pass would only add latency to whoever awaited it.
        """
        if self._refill_lock.locked():
            logger.debug("[warmpool] refill already in progress; skipping this trigger")
            return 0
        warmed = 0
        async with self._refill_lock:
            while len(self._pods) < self._size:
                outcome = await self._warm_one()
                if outcome == _WARM_SKIPPED:
                    break
                if outcome == _WARM_FAILED:
                    self._backoff_s = min(_BACKOFF_CAP_S, (self._backoff_s * 2) or _BACKOFF_START_S)
                    logger.warning(
                        "[warmpool] warm-up failed; backing off {s}s before the next attempt",
                        s=self._backoff_s,
                    )
                    break
                self._backoff_s = 0
                warmed += 1
        await self._publish_size_metrics()
        return warmed

    async def _warm_one(self) -> str:
        """Create one Pod, run the two-phase worker, and pool it when ready.

        Returns one of ``_WARM_OK`` / ``_WARM_FAILED`` / ``_WARM_SKIPPED``.
        """
        if not await self._db_capacity_available():
            return _WARM_SKIPPED
        pod_name = new_pod_name()
        placeholder = new_placeholder_session_id()
        handle = None
        stream = None
        try:
            await asyncio.to_thread(self._store.insert_warming, pod_name, placeholder)
        except Exception as e:  # noqa: BLE001
            logger.warning("[warmpool] insert warming row failed: {err}", err=e)
            return _WARM_FAILED
        try:
            env = dict(self._env_builder())
            handle = await self._spawner.spawn(uuid4(), "", env, warm=True, pod_name=pod_name)
            from kimi_cli.web.spawner.cci_exec import WARM_EXEC_COMMAND  # noqa: PLC0415

            stream = await self._spawner.attach(handle, command=WARM_EXEC_COMMAND)
            ready = await self._await_frame(stream, wp.FRAME_READY, timeout=self._ready_timeout_s)
            if ready is None or not ready.get("ok"):
                reason = (ready or {}).get("reason") or "warm_ready_timeout"
                logger.error(
                    "[warmpool] pod {pod} never became usable (reason={r}); discarding",
                    pod=pod_name,
                    r=reason,
                )
                await self._fail_pod(pod_name, handle, stream, reason=str(reason))
                return _WARM_FAILED
            # 🔴 ``ok`` is the worker's own verification that the agent spec is on
            # disk — not merely "the process started". _fetch_sandbox_assets logs
            # download failures without raising, so a Pod can boot fine and still
            # be unable to serve any claim.
            claimed_ready = await asyncio.to_thread(
                self._store.mark_ready,
                pod_name,
                pod_ip=(handle.meta or {}).get("pod_ip"),
                endpoint=handle.network_endpoint,
            )
            if not claimed_ready:
                logger.warning(
                    "[warmpool] pod {pod} lost the warming→ready race (row no longer "
                    "warming); discarding the Pod",
                    pod=pod_name,
                )
                await self._release_pod(handle, stream)
                return _WARM_FAILED
            async with self._lock:
                self._pods[pod_name] = WarmPod(pod_name, handle, stream, self._agent)
            logger.info(
                "[warmpool] pod {pod} ready (agent={a}, prepare_ms={ms})",
                pod=pod_name,
                a=ready.get("agent"),
                ms=ready.get("prepare_ms"),
            )
            return _WARM_OK
        except Exception as e:  # noqa: BLE001 — a failed warm-up is never fatal
            logger.warning("[warmpool] warm-up of {pod} failed: {err}", pod=pod_name, err=e)
            await self._fail_pod(pod_name, handle, stream, reason="warm_spawn_failed")
            return _WARM_FAILED

    # ── claim ────────────────────────────────────────────────────────────────

    async def acquire(
        self, session_id: UUID, owner_id: str, *, yolo: bool, agent: str | None, env: dict[str, str]
    ) -> tuple[SandboxHandle, Any] | None:
        """Claim a warm Pod for ``session_id``; ``None`` means "cold start".

        Order matters and is fixed (W0 spike finding #3): the caller must NOT
        replay ``initialize`` until this returns — a warm worker is blocked
        reading its bind line, and an ``initialize`` frame arriving first would
        be consumed as that line.
        """
        started = time.perf_counter()
        try:
            result = await self._acquire_inner(
                session_id, owner_id, yolo=yolo, agent=agent, env=env
            )
        finally:
            if self._metrics is not None:
                self._metrics.observe_warmpool_acquire(time.perf_counter() - started)
        if result is None:
            self._misses += 1
        else:
            self._hits += 1
        self._publish_hit_ratio()
        # Refill in the background: the user is waiting on this claim, and the
        # next session's benefit must not be paid for by this one's latency.
        with contextlib.suppress(Exception):
            asyncio.create_task(self.refill())  # noqa: RUF006
        return result

    async def _acquire_inner(
        self, session_id: UUID, owner_id: str, *, yolo: bool, agent: str | None, env: dict[str, str]
    ) -> tuple[SandboxHandle, Any] | None:
        if not owner_id:
            # Bind requires a real owner; without one the claim would have to be
            # rejected by the worker anyway (spec §4.3), so don't burn a Pod.
            logger.info("[warmpool] no owner_id for session {sid}; cold start", sid=session_id)
            return None
        if (agent or "") != self._agent:
            # The pool warms exactly one agent. Serving a session that needs a
            # different one would hand it a Pod whose downloaded spec is wrong.
            logger.info(
                "[warmpool] session {sid} needs agent {want!r} but the pool warms "
                "{have!r}; cold start",
                sid=session_id,
                want=agent,
                have=self._agent,
            )
            return None

        async with self._lock:
            reserved = next(iter(self._pods.values()), None)
            if reserved is None:
                return None
            del self._pods[reserved.pod_name]

        row = await asyncio.to_thread(self._store.claim, str(session_id), owner_id)
        if row is None:
            # Someone else (or a backend health sweep) got there first. Put the
            # Pod back: the row may have been marked dead, in which case the next
            # refill pass will notice, but a pool Pod is never dropped silently.
            async with self._lock:
                self._pods[reserved.pod_name] = reserved
            logger.info("[warmpool] claim for {sid} found no ready row; cold start", sid=session_id)
            return None
        if row.get("pod_name") != reserved.pod_name:
            # The claimed row belongs to a Pod this process does not hold (a
            # second gateway instance, or a stale row). We cannot drive it, so
            # kill the row and degrade rather than hand back a Pod we can't reach.
            logger.warning(
                "[warmpool] claimed row {got} is not the Pod we hold ({have}); "
                "marking it dead and cold-starting",
                got=row.get("pod_name"),
                have=reserved.pod_name,
            )
            await asyncio.to_thread(self._store.mark_dead, str(row.get("pod_name")), "foreign_pod")
            async with self._lock:
                self._pods[reserved.pod_name] = reserved
            return None

        bind = wp.BindRequest(
            session_id=session_id,
            owner_id=owner_id,
            yolo=yolo,
            env={k: v for k, v in env.items() if k in wp.BIND_ENV_ALLOWLIST},
        )
        async with reserved.lock:
            try:
                await reserved.stream.sendall(bind.to_frame().encode("utf-8"))
                bound = await self._await_frame(
                    reserved.stream, wp.FRAME_BOUND, timeout=self._bind_timeout_s
                )
            except Exception as e:  # noqa: BLE001 — transport failure ⇒ degrade
                bound = None
                logger.warning(
                    "[warmpool] bind write/read failed for pod {pod}: {err}",
                    pod=reserved.pod_name,
                    err=e,
                )
        if bound is None:
            await self._fail_pod(
                reserved.pod_name, reserved.handle, reserved.stream, reason="bind_failed"
            )
            logger.warning(
                "[warmpool] pod {pod} did not confirm the bind for session {sid}; cold start",
                pod=reserved.pod_name,
                sid=session_id,
            )
            return None

        logger.info(
            "[warmpool] session {sid} claimed pod {pod} (owner={owner} yolo={yolo})",
            sid=session_id,
            pod=reserved.pod_name,
            owner=owner_id,
            yolo=yolo,
        )
        await self._publish_size_metrics()
        return reserved.handle, reserved.stream

    async def release(self, pod_name: str, reason: str) -> None:
        """Retire the row of a Pod that was claimed and has now been torn down.

        The Pod itself is deleted by whoever owns it (the session process); this
        only closes the row so ``kimo_warmpool_size{phase="claimed"}`` reflects
        live sessions rather than growing forever.
        """
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                lambda: self._store.mark_dead(pod_name, reason, expect_states=(STATE_CLAIMED,))
            )
        await self._publish_size_metrics()

    # ── health ───────────────────────────────────────────────────────────────

    async def probe(self) -> dict[str, Any]:
        """Ping every pooled Pod at the worker layer; drop the ones that don't pong.

        🔴 A Pod being ``Running`` proves nothing: the worker can be wedged, or
        its assets may have failed to download. Because the pool never evicts by
        age (jie's call), this probe is the *only* eviction signal — a bad Pod
        that stays pooled fails every claim forever.

        The probe cannot use the wire protocol: it has no ``ping`` method, and a
        pre-bind worker has no soul to answer any JSON-RPC at all. Hence the warm
        handshake's own ping/pong.

        This is also what keeps the exec stream open (see :meth:`_keepalive_loop`),
        so it runs on the gateway's own timer. The backend's health-check endpoint
        calls it too, but only as an external check — never as the lifeline.
        """
        async with self._lock:
            pods = list(self._pods.values())
        results: list[dict[str, Any]] = []
        for pod in pods:
            seq = uuid4().hex[:8]
            alive = False
            detail = ""
            async with pod.lock:
                try:
                    await pod.stream.sendall(wp.encode(wp.FRAME_PING, seq=seq).encode("utf-8"))
                    pong = await self._await_frame(
                        pod.stream, wp.FRAME_PONG, timeout=self._ping_timeout_s
                    )
                    alive = pong is not None and pong.get("seq") == seq
                    if pong is not None and not alive:
                        detail = "pong seq mismatch"
                except Exception as e:  # noqa: BLE001
                    detail = f"{e.__class__.__name__}: {e}"
            if alive:
                await asyncio.to_thread(self._store.touch_health, pod.pod_name)
            else:
                logger.warning(
                    "[warmpool] pod {pod} failed the liveness probe ({d}); evicting",
                    pod=pod.pod_name,
                    d=detail or "no pong",
                )
                async with self._lock:
                    self._pods.pop(pod.pod_name, None)
                await self._fail_pod(pod.pod_name, pod.handle, pod.stream, reason="probe_failed")
            results.append({"pod_name": pod.pod_name, "alive": alive, "detail": detail})
        refilled = await self.refill()
        return {
            "size": self._size,
            "pooled": len(self._pods),
            "probed": results,
            "refilled": refilled,
        }

    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "enabled": True,
            "size": self._size,
            "agent": self._agent,
            "pooled": len(self._pods),
            "hits": self._hits,
            "misses": self._misses,
            "hit_ratio": (self._hits / total) if total else 0.0,
            "backoff_s": self._backoff_s,
        }

    # ── internals ────────────────────────────────────────────────────────────

    async def _await_frame(self, stream: Any, frame_type: str, *, timeout: float):
        """Read warm frames off ``stream`` until ``frame_type`` arrives.

        Non-warm lines (a stray log line, a diagnostic frame) are skipped rather
        than mistaken for a handshake reply. ``None`` on timeout / EOF / an
        ``error`` frame, which always means "discard this Pod".
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = await asyncio.wait_for(stream.readline(), timeout=remaining)
            except TimeoutError:
                return None
            if not line:
                return None  # EOF: the worker died
            frame = wp.decode(line)
            if frame is None:
                logger.debug("[warmpool] skipping non-warm line: {line!r}", line=line[:200])
                continue
            kind = frame.get(wp.WARM_KEY)
            if kind == frame_type:
                return frame
            if kind == wp.FRAME_ERROR:
                logger.error(
                    "[warmpool] worker reported a fatal warm error: {reason} {detail}",
                    reason=frame.get("reason"),
                    detail=frame.get("detail"),
                )
                return None

    async def _db_capacity_available(self) -> bool:
        """Is there room for one more Pod according to the DB?

        The in-memory counters only see this process. ``kimo_sandbox_pod`` is
        shared by every writer (a second gateway instance, the backend sweep),
        and a ``warming`` row exists from the first moment of a warm-up — so the
        table, not the manager, is the authority on how many Pods are live.

        A DB that cannot be read must not stop the pool from working: on error
        this returns True and the in-memory guard stands alone.
        """
        try:
            live = await asyncio.to_thread(self._store.list_by_states, (STATE_WARMING, STATE_READY))
        except Exception as e:  # noqa: BLE001 — never block a warm-up on metrics-grade IO
            logger.debug("[warmpool] capacity check unavailable ({err}); proceeding", err=e)
            return True
        if len(live) >= self._size:
            logger.warning(
                "[warmpool] {n} live row(s) (warming+ready) already at size={s}; "
                "skipping this warm-up (names={names})",
                n=len(live),
                s=self._size,
                names=", ".join(str(r.get("pod_name")) for r in live[:5]),
            )
            return False
        return True

    async def _fail_pod(self, pod_name: str, handle: Any, stream: Any, *, reason: str) -> None:
        """Mark the row dead and delete the Pod (best-effort, never raises)."""
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._store.mark_dead, pod_name, reason)
        await self._release_pod(handle, stream)
        await self._publish_size_metrics()

    async def _release_pod(self, handle: Any, stream: Any) -> None:
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()
        if handle is not None:
            with contextlib.suppress(Exception):
                await self._spawner.stop(handle)

    async def _discard(self, pod: WarmPod, *, reason: str) -> None:
        await self._fail_pod(pod.pod_name, pod.handle, pod.stream, reason=reason)

    async def _publish_size_metrics(self) -> None:
        """Publish ``kimo_warmpool_size{phase}`` from the DB (the single truth).

        Counting local Pods instead would silently hide exactly the drift this
        metric exists to expose (rows the backend marked dead, rows another
        writer created).
        """
        if self._metrics is None:
            return
        try:
            counts = await asyncio.to_thread(self._store.counts)
        except Exception as e:  # noqa: BLE001 — metrics never break the caller
            logger.debug("[warmpool] size metrics unavailable: {err}", err=e)
            return
        for phase in (STATE_WARMING, STATE_READY, STATE_CLAIMED, STATE_DEAD):
            self._metrics.set_warmpool_size(phase=phase, value=counts.get(phase, 0))

    def _publish_hit_ratio(self) -> None:
        if self._metrics is None:
            return
        total = self._hits + self._misses
        if total:
            self._metrics.set_warmpool_hit_ratio(self._hits / total)


__all__ = ["WarmPod", "WarmPoolManager"]
