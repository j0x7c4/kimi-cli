"""``kimo_sandbox_pod`` row access for the warm pool (hechun fork).

# hechun-fork-cci (warm pool, plan 2026-09-07-sandbox-warmpool-plan.md §0)

🔴 **Schema is owned by backend Flyway (V40).** Same rule as
``storage/my_storage.py``: kimo never creates, alters or migrates a table — it
only reads and writes rows. Nothing in this module issues DDL.

Row shape (authoritative definition lives in the backend migration):

    kimo_sandbox_pod(
        pod_name        VARCHAR(128) PRIMARY KEY,   -- kimo-sandbox-{uuid}
        state           VARCHAR(16)  NOT NULL,      -- warming|ready|claimed|dead
        kimo_session_id CHAR(36)     NULL,          -- placeholder while warming
        owner_id        VARCHAR(128) NULL,
        pod_ip          VARCHAR(64)  NULL,
        endpoint        VARCHAR(255) NULL,
        dead_reason     VARCHAR(64)  NULL,
        created_at      DATETIME(6)  NOT NULL,
        claimed_at      DATETIME(6)  NULL,
        last_health_at  DATETIME(6)  NULL)

Two writers touch these rows concurrently (this gateway claiming, backend's
scheduler marking dead), so **every** transition below is a conditional UPDATE
that also asserts the state it expects to leave. Whoever commits first wins; the
loser sees 0 affected rows and takes its own fallback path — no locks, no queue
component (plan §0.2/§0.3).

All calls are blocking SQLAlchemy; the manager runs them off the event loop.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from kimi_cli import logger

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

TABLE = "kimo_sandbox_pod"

STATE_WARMING = "warming"
STATE_READY = "ready"
STATE_CLAIMED = "claimed"
STATE_DEAD = "dead"

#: Reserved-UUID prefix for the placeholder ``kimo_session_id`` a warm row
#: carries before it is claimed. The column is CHAR(36), so a non-UUID marker
#: like ``warm-x`` is not an option (plan §0.4).
#:
#: 🔴 The placeholder is a *carrier*, never a predicate: nothing may decide
#: "is this pod warm?" by pattern-matching a session id. State lives in the
#: ``state`` column, so a claim flips warm→claimed inside one transaction and
#: there is no window in which a half-written value could grant a session some
#: permanent special treatment (spec §7).
PLACEHOLDER_PREFIX = "00000000-0000-4000-8000-"


def new_placeholder_session_id() -> str:
    """A fresh reserved-segment placeholder UUID string (36 chars)."""
    return PLACEHOLDER_PREFIX + uuid.uuid4().hex[:12]


def new_pod_name() -> str:
    """A warm Pod name.

    Keeps the ``kimo-sandbox-`` prefix (the orphan reconciler and the metrics
    Pod count both key off it) but drops the "name == session id" convention:
    a warm Pod has no session id yet, and after a claim its name still won't
    match the session it serves. The DB row is the mapping (spec §5).
    """
    return f"kimo-sandbox-{uuid.uuid4()}"


#: MySQL error code for "deadlock found; transaction was rolled back"
#: (``ER_LOCK_DEADLOCK``).
#:
#: 🔴 W1 measured this for real: 60 concurrent claims produced 39 deadlocks
#: (`KimoSandboxPodClaimIT`). The claim UPDATE walks the secondary index
#: ``idx_ksp_claimable`` and then goes back to the primary key, so two
#: connections can take the two locks in opposite order. InnoDB rolls one back
#: and commits the other, so "exactly one winner" still holds — but the loser
#: MUST be normalised into the same "claim failed" path as "0 rows affected"
#: (plan §0.3). Letting it escape as an exception would turn a silent, expected
#: degradation into a user-visible AI-assistant error.
_ER_LOCK_DEADLOCK = 1213


def _is_deadlock(exc: BaseException) -> bool:
    """Whether ``exc`` is a MySQL 1213 deadlock (through any driver wrapper).

    Deliberately narrow: only errno 1213 is swallowed as "lost the race". Every
    other DB error still propagates, because silently treating (say) a
    connection failure as "pool empty" would hide a real outage behind a
    permanently cold start.

    Three links are followed because the errno can sit at any of them:
    SQLAlchemy raises its own ``OperationalError`` whose args are strings, keeps
    the driver exception on ``.orig``, and chains it via ``__cause__``. Matching
    on message text instead would be fragile across driver versions, so the test
    is strictly on the integer errno.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        cur = pending.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        for arg in getattr(cur, "args", ()):
            if isinstance(arg, int) and arg == _ER_LOCK_DEADLOCK:
                return True
            if isinstance(arg, (tuple, list)) and arg and arg[0] == _ER_LOCK_DEADLOCK:
                return True
        for link in (getattr(cur, "orig", None), cur.__cause__, cur.__context__):
            if isinstance(link, BaseException):
                pending.append(link)
    return False


class WarmPoolStore:
    """Row-level operations on ``kimo_sandbox_pod`` (blocking; call off-loop)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── writes ───────────────────────────────────────────────────────────────

    def insert_warming(self, pod_name: str, placeholder_session_id: str) -> None:
        """Record a Pod that is being warmed up."""
        from sqlalchemy import text  # noqa: PLC0415

        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {TABLE} "
                    "(pod_name, state, kimo_session_id, created_at) "
                    "VALUES (:pod, :state, :sid, now(6))"
                ),
                {"pod": pod_name, "state": STATE_WARMING, "sid": placeholder_session_id},
            )

    def mark_ready(self, pod_name: str, *, pod_ip: str | None, endpoint: str | None) -> bool:
        """``warming → ready``. False when the row was no longer warming.

        Losing this race means someone (backend health check / restart
        reconcile) already declared the Pod dead — the caller must then discard
        the Pod instead of serving it.
        """
        from sqlalchemy import text  # noqa: PLC0415

        try:
            with self._engine.begin() as conn:
                res = conn.execute(
                    text(
                        f"UPDATE {TABLE} SET state=:ready, pod_ip=:ip, endpoint=:ep, "
                        "last_health_at=now(6) "
                        "WHERE pod_name=:pod AND state=:warming"
                    ),
                    {
                        "ready": STATE_READY,
                        "warming": STATE_WARMING,
                        "ip": pod_ip,
                        "ep": endpoint,
                        "pod": pod_name,
                    },
                )
                return res.rowcount == 1
        except Exception as e:  # noqa: BLE001 — 1213 only; see _is_deadlock
            if _is_deadlock(e):
                logger.info(
                    "[warmpool] mark_ready({pod}) lost an InnoDB deadlock (1213); "
                    "discarding the Pod instead of pooling it",
                    pod=pod_name,
                )
                return False
            raise

    def claim(self, session_id: str, owner_id: str, pod_name: str) -> dict[str, Any] | None:
        """Atomically claim ``pod_name`` — the Pod the caller reserved — for ``session_id``.

        This is the frozen contract from plan §0.3 — the ONE legal way to claim:

            UPDATE kimo_sandbox_pod
               SET state='claimed', kimo_session_id=?, owner_id=?, claimed_at=now(6)
             WHERE state='ready' AND owner_id IS NULL AND pod_name=?
             ORDER BY created_at LIMIT 1

        A single conditional single-row UPDATE *is* the mutual exclusion: with N
        concurrent claimers exactly one gets ``rowcount == 1``; everyone else
        gets 0 and degrades to a cold start (which is a normal path, not an
        error, and must not alert).

        🔴 ``pod_name`` is not an optimisation — it is what makes the row the
        caller wins the *same* Pod it holds in memory (2026-09-07 review, HIGH).
        Claiming "any ready row" let two concurrent claimers each win the
        other's row: both then took the ``foreign_pod`` branch, marked both rows
        dead, and left two healthy Pods stranded in ``_pods`` — a pool that can
        neither serve nor refill while both Pods keep billing.

        Returns the claimed row, or ``None`` when this Pod was no longer
        claimable (already taken, marked dead by the backend sweep, or gone).
        The row is re-read inside the same transaction **by ``pod_name``** so
        the read is deterministic even if a stale ``claimed`` row for the same
        session survived a failed release.
        """
        from sqlalchemy import text  # noqa: PLC0415

        try:
            return self._claim_once(text, session_id, owner_id, pod_name)
        except Exception as e:  # noqa: BLE001 — only 1213 is swallowed, see below
            if _is_deadlock(e):
                logger.info(
                    "[warmpool] claim for {sid} lost an InnoDB deadlock (1213); "
                    "treating it as a pool miss and cold-starting",
                    sid=session_id,
                )
                return None
            raise

    def _claim_once(
        self, text: Any, session_id: str, owner_id: str, pod_name: str
    ) -> dict[str, Any] | None:
        with self._engine.begin() as conn:
            res = conn.execute(
                text(
                    f"UPDATE {TABLE} "
                    "SET state='claimed', kimo_session_id=:sid, owner_id=:owner, "
                    "claimed_at=now(6) "
                    "WHERE state='ready' AND owner_id IS NULL AND pod_name=:pod "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"sid": session_id, "owner": owner_id, "pod": pod_name},
            )
            if res.rowcount != 1:
                return None
            row = (
                conn.execute(
                    text(
                        f"SELECT pod_name, state, kimo_session_id, owner_id, pod_ip, endpoint "
                        f"FROM {TABLE} WHERE pod_name=:pod AND state='claimed'"
                    ),
                    {"pod": pod_name},
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    def mark_dead(
        self, pod_name: str, reason: str, *, expect_states: tuple[str, ...] | None = None
    ) -> bool:
        """Mark one row dead. ``expect_states`` makes the transition conditional."""
        from sqlalchemy import text  # noqa: PLC0415

        params: dict[str, Any] = {"pod": pod_name, "reason": reason[:64], "dead": STATE_DEAD}
        sql = f"UPDATE {TABLE} SET state=:dead, dead_reason=:reason WHERE pod_name=:pod"
        if expect_states:
            placeholders: list[str] = []
            for i, st in enumerate(expect_states):
                key = f"s{i}"
                params[key] = st
                placeholders.append(f":{key}")
            sql += f" AND state IN ({', '.join(placeholders)})"
        try:
            with self._engine.begin() as conn:
                return conn.execute(text(sql), params).rowcount == 1
        except Exception as e:  # noqa: BLE001 — 1213 only; see _is_deadlock
            if _is_deadlock(e):
                logger.info(
                    "[warmpool] mark_dead({pod}) lost an InnoDB deadlock (1213); "
                    "treating it as 'someone else won'",
                    pod=pod_name,
                )
                return False
            raise

    def mark_all_live_dead(self, reason: str) -> list[str]:
        """Mark every ``warming``/``ready`` row dead; return the affected pod names.

        🔴 Called on gateway startup, *before* refilling. ``reconcile_orphans``
        deletes every ``kimo-sandbox-*`` Pod in the namespace — warm Pods
        included — so leaving their rows ``ready`` would let a later claim
        succeed against a Pod that no longer exists. That is strictly worse than
        an empty pool: the claim reports success and hands the user a session
        wired to nothing, bypassing the cold-start fallback entirely (spec §9).
        """
        from sqlalchemy import text  # noqa: PLC0415

        with self._engine.begin() as conn:
            names = [
                r[0]
                for r in conn.execute(
                    text(f"SELECT pod_name FROM {TABLE} WHERE state IN (:warming, :ready)"),
                    {"warming": STATE_WARMING, "ready": STATE_READY},
                ).all()
            ]
            if names:
                conn.execute(
                    text(
                        f"UPDATE {TABLE} SET state=:dead, dead_reason=:reason "
                        "WHERE state IN (:warming, :ready)"
                    ),
                    {
                        "dead": STATE_DEAD,
                        "reason": reason[:64],
                        "warming": STATE_WARMING,
                        "ready": STATE_READY,
                    },
                )
        return names

    def touch_health(self, pod_name: str) -> None:
        """Record a successful liveness probe."""
        from sqlalchemy import text  # noqa: PLC0415

        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {TABLE} SET last_health_at=now(6) WHERE pod_name=:pod"),
                    {"pod": pod_name},
                )
        except Exception as e:  # noqa: BLE001 — bookkeeping must not fail a probe
            logger.warning("[warmpool] touch_health failed pod={pod}: {err}", pod=pod_name, err=e)

    # ── reads ────────────────────────────────────────────────────────────────

    def list_by_states(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        from sqlalchemy import text  # noqa: PLC0415

        keys = {f"s{i}": st for i, st in enumerate(states)}
        placeholders = ", ".join(f":{k}" for k in keys)
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        f"SELECT pod_name, state, kimo_session_id, owner_id, pod_ip, endpoint, "
                        f"created_at, claimed_at, last_health_at, dead_reason "
                        f"FROM {TABLE} WHERE state IN ({placeholders}) ORDER BY created_at"
                    ),
                    keys,
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        """``state → row count`` (drives ``kimo_warmpool_size{phase}``)."""
        from sqlalchemy import text  # noqa: PLC0415

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT state, COUNT(*) AS n FROM {TABLE} GROUP BY state")
            ).all()
        return {str(state): int(n) for state, n in rows}


__all__ = [
    "PLACEHOLDER_PREFIX",
    "STATE_CLAIMED",
    "STATE_DEAD",
    "STATE_READY",
    "STATE_WARMING",
    "TABLE",
    "WarmPoolStore",
    "new_placeholder_session_id",
    "new_pod_name",
]
