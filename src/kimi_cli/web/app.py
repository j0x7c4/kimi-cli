"""Kimi Code CLI Web UI application."""

import asyncio
import os
import secrets
import sys
import time
import webbrowser
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import scalar_fastapi
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.responses import HTMLResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kimi_cli import logger
from kimi_cli.utils.server import (
    find_available_port,
    format_url,
    get_network_addresses,
    is_local_host,
)
from kimi_cli.web.api import (
    admin_router,
    agents_router,
    auth_router,
    branding_admin_router,
    branding_public_router,
    capabilities_router,
    config_router,
    memory_router,
    open_in_router,
    sandbox_assets_router,
    sessions_router,
    work_dirs_router,
)
from kimi_cli.web.auth import (
    DEFAULT_ALLOWED_ORIGIN_REGEX,
    AuthMiddleware,
    is_private_ip,
    normalize_allowed_origins,
)
from kimi_cli.web.runner.process import KimiCLIRunner

# Container mode imports
try:
    from kimi_cli.web.runner.container import ContainerRunner
except Exception:
    ContainerRunner = None  # type: ignore[misc,assignment]

# Configure logging based on LOG_LEVEL environment variable
_log_level = os.environ.get("LOG_LEVEL", "WARNING").upper()
logger.remove()
logger.enable("kimi_cli")
logger.add(sys.stderr, level=_log_level)

# scalar-fastapi does not ship typing stubs.
get_scalar_api_reference = cast(  # pyright: ignore[reportUnknownMemberType]
    Callable[..., HTMLResponse],
    scalar_fastapi.get_scalar_api_reference,  # pyright: ignore[reportUnknownMemberType]
)

# Constants
STATIC_DIR = Path(__file__).parent / "static"
GZIP_MINIMUM_SIZE = 1024
GZIP_COMPRESSION_LEVEL = 6
DEFAULT_PORT = 5494
MAX_PORT_ATTEMPTS = 10
ENV_SESSION_TOKEN = "KIMI_WEB_SESSION_TOKEN"
ENV_ALLOWED_ORIGINS = "KIMI_WEB_ALLOWED_ORIGINS"
ENV_ENFORCE_ORIGIN = "KIMI_WEB_ENFORCE_ORIGIN"
ENV_RESTRICT_SENSITIVE_APIS = "KIMI_WEB_RESTRICT_SENSITIVE_APIS"
ENV_MAX_PUBLIC_PATH_DEPTH = "KIMI_WEB_MAX_PUBLIC_PATH_DEPTH"

# Cache durations
_IMMUTABLE_MAX_AGE = 365 * 24 * 3600  # 1 year for content-hashed assets


class _StaticCacheHeadersMiddleware:
    """Inject Cache-Control headers for static assets served by Starlette.

    * ``index.html`` (and any non-hashed HTML) → ``no-cache`` so the browser
      always revalidates, preventing stale references to renamed chunks after a
      CLI upgrade (see #1602).
    * Hashed assets under ``/assets/`` → long-lived ``immutable`` cache because
      the content hash in the filename already guarantees uniqueness.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        async def _send_with_cache_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if path.startswith("/assets/"):
                    headers["cache-control"] = f"public, max-age={_IMMUTABLE_MAX_AGE}, immutable"
                elif path == "/" or path.endswith(".html"):
                    headers["cache-control"] = "no-cache, no-store, must-revalidate"
            await send(message)

        await self.app(scope, receive, _send_with_cache_headers)


def _get_private_addresses(addresses: list[str]) -> list[str]:
    """Filter addresses to only include private IPs."""
    return [ip for ip in addresses if is_private_ip(ip)]


def _load_env_flag(key: str) -> bool:
    return os.environ.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_env(key: str, *, default: bool) -> bool:
    """Read a boolean env var with an explicit default (None-safe).

    Unlike :func:`_load_env_flag` (which defaults to False), this honours a
    caller-supplied default so a flag can default to *on* and be turned off only
    by an explicit falsy value.
    """
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(key: str, *, default: int) -> int:
    """Read a positive-int env var, falling back to ``default`` on bad input."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip().isdigit():
        return default
    val = int(raw.strip())
    return val if val > 0 else default


ENV_LAN_ONLY = "KIMI_WEB_LAN_ONLY"

# hechun-fork-cci: gateway-side CCI Pod 生命周期兜底 env（仅 CCI 模式生效）。
_ENV_RECONCILE_ON_STARTUP = "KIMI_CCI_RECONCILE_ON_STARTUP"  # 默认开（单实例假设）
_ENV_SWEEP_INTERVAL = "KIMI_CCI_SWEEP_INTERVAL_SECONDS"  # 默认 60s
_ENV_IDLE_TTL = "KIMI_CCI_IDLE_TTL_SECONDS"  # 默认 900s = 15min


async def _reclaim_idle_cci_sessions(runner: Any, *, idle_ttl_s: int) -> int:
    """One sweep pass: stop_worker() every CCI session idle past ``idle_ttl_s``.

    hechun-fork-cci. A session is reclaimed iff ALL hold:
      • it is CCI-backed (has a ``handle`` attribute → CCISessionProcess);
      • its worker is alive AND there is currently a sandbox handle (a spawn that
        already completed — never mid-spawn, where handle is still None);
      • status.state is ``idle`` (NOT busy, NOT restarting, NOT stopped/error);
      • ``now - last_active_at > idle_ttl_s``.

    Under CCI ``stop_worker`` deletes the Pod (billing stops). Returns the number
    of sessions reclaimed. Never raises — per-session failures are logged + skipped
    so one bad session can't abort the sweep.
    """
    now = time.monotonic()
    reclaimed = 0
    try:
        sessions = runner.iter_sessions()
    except Exception as e:  # noqa: BLE001 — enumeration failure must not crash sweeper
        logger.warning("[cci-sweeper] iter_sessions failed: {err}", err=e)
        return 0

    for sid, proc in sessions:
        try:
            # CCI-backed only: docker/local SessionProcess has no ``handle``.
            handle = getattr(proc, "handle", None)
            if handle is None:
                # Either not CCI, or CCI but not (yet) spawned / mid-spawn → skip.
                # Skipping mid-spawn (handle still None) is the safety guarantee
                # that we never reclaim a session while spawn is in progress.
                continue
            if not proc.is_alive:
                continue
            # Only reclaim genuinely idle sessions. busy = prompt in flight;
            # restarting = config-reload in progress; both must be left alone.
            if proc.status.state != "idle":
                continue
            last = proc.last_active_at
            if last is None or (now - last) <= idle_ttl_s:
                continue
            idle_for = int(now - last)
            logger.info(
                "[cci-sweeper] reclaiming idle session {sid} (idle {s}s > ttl)",
                sid=sid,
                s=idle_for,
            )
            await proc.stop_worker(reason="idle_reclaim")
            reclaimed += 1
        except Exception as e:  # noqa: BLE001 — one bad session can't abort the sweep
            logger.warning(
                "[cci-sweeper] failed to reclaim session {sid}: {err}", sid=sid, err=e
            )
    return reclaimed


async def _cci_idle_sweeper(runner: Any, *, interval_s: int, idle_ttl_s: int) -> None:
    """Background loop: every ``interval_s`` reclaim idle CCI sessions.

    hechun-fork-cci. Lives for the app lifespan (cancelled on shutdown). Each pass
    is wrapped so any exception is swallowed + logged — the sweeper must never die
    and take the gateway down with it. CancelledError propagates so lifespan can
    cleanly tear it down.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            n = await _reclaim_idle_cci_sessions(runner, idle_ttl_s=idle_ttl_s)
            if n:
                logger.info("[cci-sweeper] reclaimed {n} idle CCI session(s)", n=n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — never let the sweeper crash
            logger.warning("[cci-sweeper] sweep pass errored (continuing): {err}", err=e)


def create_app(
    session_token: str | None = None,
    allowed_origins: list[str] | None = None,
    enforce_origin: bool | None = None,
    restrict_sensitive_apis: bool | None = None,
    max_public_path_depth: int | None = None,
    lan_only: bool | None = None,
) -> FastAPI:
    """Create the FastAPI application for Kimi CLI web UI."""

    env_token = os.environ.get(ENV_SESSION_TOKEN) or None
    env_origins = normalize_allowed_origins(os.environ.get(ENV_ALLOWED_ORIGINS))
    env_enforce_origin = _load_env_flag(ENV_ENFORCE_ORIGIN)
    env_restrict_sensitive = _load_env_flag(ENV_RESTRICT_SENSITIVE_APIS)
    env_max_depth_str = os.environ.get(ENV_MAX_PUBLIC_PATH_DEPTH)
    env_max_depth = (
        int(env_max_depth_str) if env_max_depth_str and env_max_depth_str.isdigit() else None
    )
    env_lan_only = _load_env_flag(ENV_LAN_ONLY)

    session_token = session_token if session_token is not None else env_token
    allowed_origins = allowed_origins if allowed_origins is not None else env_origins
    enforce_origin = enforce_origin if enforce_origin is not None else env_enforce_origin
    restrict_sensitive_apis = (
        restrict_sensitive_apis if restrict_sensitive_apis is not None else env_restrict_sensitive
    )
    max_public_path_depth = (
        max_public_path_depth if max_public_path_depth is not None else env_max_depth
    )
    lan_only = lan_only if lan_only is not None else env_lan_only

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Initialise user database (creates tables + default admin if needed)
        from kimi_cli.web.db.database import init_db

        try:
            await asyncio.to_thread(init_db)
        except Exception as _e:  # pragma: no cover
            logger.warning("Failed to initialise user database: {err}", err=_e)

        # Prefer the user-configured default work dir over the process CWD.
        # When launched via the macOS .app bundle, uvicorn runs with cwd set
        # to ~/Library/Application Support/<AppName>, which is not what the
        # user thinks of as their starting directory.
        app.state.startup_dir = os.environ.get("KIMI_DEFAULT_WORK_DIR") or os.getcwd()
        app.state.session_token = session_token
        app.state.allowed_origins = allowed_origins
        app.state.enforce_origin = enforce_origin
        app.state.restrict_sensitive_apis = restrict_sensitive_apis
        app.state.max_public_path_depth = max_public_path_depth
        app.state.lan_only = lan_only

        # Storage backend (spec §2.4.2.C): file (default) or postgres. Selected
        # by env KIMI_STORAGE_BACKEND; downstream task ⑥/⑦/⑧ will switch
        # session_state / archivist / sessions to consume app.state.kimo_storage
        # instead of calling file IO helpers directly. Failure here is fatal:
        # a misconfigured pg URL must not silently fall back to file IO.
        from kimi_cli.storage import build_storage

        app.state.kimo_storage = build_storage()
        logger.info(
            "[create_app] kimo_storage backend={cls}",
            cls=type(app.state.kimo_storage).__name__,
        )

        # hechun-fork-cci: select sandbox spawner by KIMI_SPAWNER_BACKEND (spec
        # §C-9). 'docker' (default) keeps the existing ContainerRunner path and
        # returns None — purely additive. 'cci' builds CCISpawner from
        # HUAWEICLOUD_* env. WarmPoolManager / select_spawner gray-routing (06-09
        # plan) plug into app.state.spawner when present.
        from kimi_cli.web.spawner import build_spawner

        try:
            app.state.spawner = build_spawner()
        except Exception as _e:  # noqa: BLE001
            # A misconfigured CCI spawner must surface loudly but not before the
            # storage init result; re-raise to fail fast (no silent docker fallback
            # when cci was explicitly requested).
            logger.error("[create_app] spawner init failed: {err}", err=_e)
            raise
        _spawner_backend = (os.environ.get("KIMI_SPAWNER_BACKEND") or "docker").strip().lower()
        logger.info("[create_app] spawner backend={b}", b=_spawner_backend)

        # hechun-fork-cci: mount /metrics (spec §7.2). Wire the CCI client into
        # the metrics state so the IP-pool gauge can poll read_network.
        app.state.metrics = None
        if _load_env_flag("KIMI_METRICS_ENABLED"):
            try:
                from kimi_cli.web.metrics import MetricsState

                metrics_state = MetricsState()
                spawner = getattr(app.state, "spawner", None)
                if spawner is not None and hasattr(spawner, "client"):
                    metrics_state.cci_client = spawner.client
                    metrics_state.namespace = getattr(
                        spawner, "namespace", metrics_state.namespace
                    )
                    # Backfill the runtime metrics sink into the spawner (spec §7.2):
                    # this wires kimo_sandbox_spawn_* / kimo_active_sandboxes (in
                    # CCISpawner.spawn/stop) and forwards into the REST client so
                    # kimo_cci_api_error_total is emitted at its raise point. No-op
                    # for any spawner lacking a ``metrics`` attribute (docker path).
                    if hasattr(spawner, "metrics"):
                        spawner.metrics = metrics_state
                app.state.metrics = metrics_state
                logger.info("[create_app] metrics on (/metrics mounted)")
            except Exception as _e:  # noqa: BLE001
                logger.warning("[create_app] metrics init failed: {err}", err=_e)

        # Start KimiCLI runner. Selection order (hechun-fork-cci):
        #   1. spawner is CCI (KIMI_SPAWNER_BACKEND=cci) → CCIRunner: worker runs
        #      in a CCI Pod, driven over the exec WebSocket (same read loop /
        #      send path as docker via SessionProcess transport primitives).
        #   2. KIMI_USE_CONTAINERS → ContainerRunner (docker run -i --rm).
        #   3. else → KimiCLIRunner (local subprocess).
        use_containers = _load_env_flag("KIMI_USE_CONTAINERS")
        sweeper_task: asyncio.Task[None] | None = None
        if _spawner_backend == "cci" and app.state.spawner is not None:
            from kimi_cli.web.runner.cci_process import CCIRunner

            runner = CCIRunner(spawner=app.state.spawner)
            logger.info("[create_app] runner=CCIRunner (exec WebSocket)")

            # hechun-fork-cci: gateway-side Pod 生命周期兜底（不依赖 backend 正确性）。
            #
            # ⚠️ 单 gateway 实例假设：启动孤儿清扫(reconcile_orphans)会删掉整个
            # namespace 里所有 kimo-sandbox-* Pod。多副本共享同一 namespace 时，启动
            # 清扫会误删别副本正在服务的 Pod —— 多副本部署务必设
            # KIMI_CCI_RECONCILE_ON_STARTUP=0 关掉本清扫。
            if _bool_env(_ENV_RECONCILE_ON_STARTUP, default=True):
                try:
                    n = await app.state.spawner.reconcile_orphans()
                    logger.info("[create_app] reconcile_orphans 删除 {n} 个孤儿 Pod", n=n)
                except Exception as _e:  # noqa: BLE001 — 清扫失败不阻塞启动
                    logger.warning("[create_app] reconcile_orphans 失败（忽略）: {err}", err=_e)
        elif use_containers and ContainerRunner is not None:
            runner = ContainerRunner(
                image=os.environ.get("SANDBOX_IMAGE", "kimi-agent-sandbox:latest"),
                network=os.environ.get("DOCKER_NETWORK"),
                resource_limits={
                    "cpus": os.environ.get("SANDBOX_CPU_LIMIT", "2"),
                    "memory": os.environ.get("SANDBOX_MEMORY_LIMIT", "4g"),
                    "pids": os.environ.get("SANDBOX_PID_LIMIT", "1000"),
                },
            )
        else:
            runner = KimiCLIRunner()
        app.state.runner = runner
        runner.start()

        # hechun-fork-cci: idle 兜底 sweeper —— 仅 CCI 模式启。周期回收"worker 活着但
        # 空闲超时"的 session（防 Pod 永久 Running 计费），是对 backend SandboxBroker
        # 的 gateway 侧兜底（防其失效）。docker/local 不启。
        if _spawner_backend == "cci" and app.state.spawner is not None:
            interval = _int_env(_ENV_SWEEP_INTERVAL, default=60)
            idle_ttl = _int_env(_ENV_IDLE_TTL, default=900)
            sweeper_task = asyncio.create_task(
                _cci_idle_sweeper(runner, interval_s=interval, idle_ttl_s=idle_ttl)
            )
            logger.info(
                "[create_app] CCI idle sweeper on interval={i}s idle_ttl={t}s",
                i=interval,
                t=idle_ttl,
            )

        try:
            yield
        finally:
            if sweeper_task is not None:
                sweeper_task.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper_task
            await runner.stop()

    application = FastAPI(
        title="Kimi Code CLI Web Interface",
        docs_url=None,
        lifespan=lifespan,
        separate_input_output_schemas=False,
    )

    application.add_middleware(
        cast(Any, GZipMiddleware),
        minimum_size=GZIP_MINIMUM_SIZE,
        compresslevel=GZIP_COMPRESSION_LEVEL,
    )

    application.add_middleware(cast(Any, _StaticCacheHeadersMiddleware))

    application.add_middleware(
        cast(Any, AuthMiddleware),
        session_token=session_token,
        allowed_origins=allowed_origins,
        enforce_origin=enforce_origin,
        lan_only=lan_only,
    )

    cors_kwargs: dict[str, Any] = {
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
    if allowed_origins:
        cors_kwargs["allow_origins"] = allowed_origins
    else:
        cors_kwargs["allow_origin_regex"] = DEFAULT_ALLOWED_ORIGIN_REGEX.pattern

    # CORS middleware for local development
    application.add_middleware(cast(Any, CORSMiddleware), **cors_kwargs)

    application.include_router(auth_router)
    application.include_router(admin_router)
    application.include_router(branding_public_router)
    application.include_router(branding_admin_router)
    application.include_router(config_router)
    application.include_router(sessions_router)
    application.include_router(work_dirs_router)
    application.include_router(agents_router)
    application.include_router(memory_router)
    application.include_router(capabilities_router)
    # hechun-fork-cci: internal endpoint that ships static sandbox assets
    # (~/.kimi/agents etc.) to CCI Pod workers, which cannot bind-mount the
    # gateway host. The handler verifies the gateway session token itself
    # (the path is outside /api/ so AuthMiddleware does not gate it).
    application.include_router(sandbox_assets_router)
    if not restrict_sensitive_apis:
        application.include_router(open_in_router)

    # hechun-fork-cci: /metrics router (spec §7.2). Registered at build time; the
    # handler returns 503 until lifespan sets app.state.metrics (i.e. only emits
    # when KIMI_METRICS_ENABLED). prometheus_client is imported lazily inside the
    # handler so the docker path doesn't require it.
    if _load_env_flag("KIMI_METRICS_ENABLED"):
        try:
            from kimi_cli.web.metrics import build_metrics_router

            application.include_router(build_metrics_router())
        except Exception as _e:  # noqa: BLE001
            logger.warning("Failed to mount /metrics router: {err}", err=_e)

    @application.get("/scalar", include_in_schema=False)
    @application.get("/docs", include_in_schema=False)
    async def scalar_html() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return get_scalar_api_reference(
            openapi_url=application.openapi_url or "",
            title=application.title,
        )

    @application.get("/healthz")
    async def health_probe() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Health check endpoint."""
        return {"status": "ok"}

    # SPA catch-all: handles both static assets and client-side routes.
    #
    # Why not StaticFiles mount alone:
    #   @app.get routes always take priority over application.mount() in
    #   FastAPI/Starlette, so once a catch-all route exists the mount is never
    #   reached. We therefore serve static files directly here and fall back to
    #   index.html for any path that isn't a real file (SPA client routes like
    #   /admin).
    _index_html = STATIC_DIR / "index.html"
    if _index_html.exists():
        from fastapi.responses import FileResponse as _FileResponse

        _spa_html_response = HTMLResponse(
            content=_index_html.read_text(encoding="utf-8"),
            headers={"cache-control": "no-cache, no-store, must-revalidate"},
        )

        # Explicit root route: /{full_path:path} does NOT match "/" in Starlette
        # because the path converter requires at least one character.
        @application.get("/", include_in_schema=False)
        async def spa_root() -> Response:  # pyright: ignore[reportUnusedFunction]
            return _spa_html_response

        @application.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> Response:  # pyright: ignore[reportUnusedFunction]
            # Serve real static files (JS, CSS, images, etc.) directly.
            candidate = STATIC_DIR / full_path
            if candidate.is_file():
                return _FileResponse(candidate)
            # Path with an extension that doesn't exist → true 404
            # (avoids serving index.html with wrong MIME type for missing assets).
            if "." in full_path.split("/")[-1]:
                from fastapi import HTTPException

                raise HTTPException(status_code=404)
            # No extension → SPA client-side route (e.g. /admin) → index.html
            return _spa_html_response

    # Mount static files as secondary fallback (reached only when the
    # catch-all above is absent, i.e. index.html does not exist yet).
    elif STATIC_DIR.exists():
        application.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return application


def run_web_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    reload: bool = False,
    open_browser: bool = True,
    auth_token: str | None = None,
    allowed_origins: str | None = None,
    dangerously_omit_auth: bool = False,
    restrict_sensitive_apis: bool | None = None,
    lan_only: bool = True,
) -> None:
    """Run the web server."""
    import sys
    import threading

    import uvicorn

    from kimi_cli.utils.server import print_banner

    public_mode = not is_local_host(host)
    parsed_allowed_origins = normalize_allowed_origins(allowed_origins)
    auto_populate_origins = public_mode and not parsed_allowed_origins

    if restrict_sensitive_apis is None:
        # Only restrict sensitive APIs in public mode (non-LAN-only)
        restrict_sensitive_apis = public_mode and not lan_only

    if public_mode and dangerously_omit_auth:
        warning_lines = [
            "SECURITY WARNING",
            "",
            "Authentication is DISABLED while running on a public host.",
            "Anyone on the network can access your sessions and files.",
            "",
            "Type 'I UNDERSTAND THE RISKS' to continue:",
        ]
        print_banner(warning_lines)
        if not sys.stdin.isatty():
            raise RuntimeError("Refusing to start without auth in non-interactive mode.")
        response = input("> ").strip()
        if response != "I UNDERSTAND THE RISKS":
            raise RuntimeError("Aborted by user.")

    if dangerously_omit_auth:
        session_token = None
    elif auth_token:
        session_token = auth_token
    elif public_mode:
        session_token = secrets.token_urlsafe(32)
    else:
        session_token = None

    if session_token:
        os.environ[ENV_SESSION_TOKEN] = session_token
    else:
        os.environ.pop(ENV_SESSION_TOKEN, None)

    # Find available port first (needed for auto-populating origins)
    actual_port = find_available_port(host, port)
    if actual_port != port:
        print(f"Port {port} is in use, using port {actual_port} instead")

    # Auto-populate allowed origins with detected network addresses + port
    if auto_populate_origins:
        auto_origins = [
            f"http://localhost:{actual_port}",
            f"http://127.0.0.1:{actual_port}",
        ]
        if host == "0.0.0.0":
            # Binding to all interfaces: add all network addresses
            network_addrs = get_network_addresses()
            for addr in network_addrs:
                auto_origins.append(format_url(addr, actual_port))
        else:
            # Explicit host specified: only add that host
            auto_origins.append(format_url(host, actual_port))
        parsed_allowed_origins = auto_origins

    if parsed_allowed_origins:
        os.environ[ENV_ALLOWED_ORIGINS] = ",".join(parsed_allowed_origins)
    else:
        os.environ.pop(ENV_ALLOWED_ORIGINS, None)

    os.environ[ENV_ENFORCE_ORIGIN] = "1" if (public_mode and not lan_only) else "0"
    os.environ[ENV_RESTRICT_SENSITIVE_APIS] = "1" if restrict_sensitive_apis else "0"
    os.environ[ENV_LAN_ONLY] = "1" if lan_only else "0"

    # Determine display URLs
    display_hosts: list[tuple[str, str]] = []
    if host == "0.0.0.0":
        # Show localhost as "Local" and network interfaces
        display_hosts.append(("Local", "localhost"))
        network_addrs = get_network_addresses()

        # In lan_only mode, only show private IPs
        if lan_only:
            network_addrs = _get_private_addresses(network_addrs)

        for addr in network_addrs:
            display_hosts.append(("Network", addr))
    else:
        # Show the specified host
        label = "Local" if is_local_host(host) else "Network"
        display_hosts.append((label, host))

    # Build URLs with token if needed
    def make_url(host_addr: str) -> tuple[str, str]:
        """Returns (url, browser_url) tuple."""
        url = format_url(host_addr, actual_port)
        browser_url = f"{url}/?token={quote(session_token)}" if session_token else url
        return url, browser_url

    # For browser opening, prefer localhost, then first network address
    browser_host = "localhost" if host == "0.0.0.0" else host
    _, browser_url = make_url(browser_host)

    if open_browser:

        def open_browser_after_delay():
            import time

            time.sleep(1.5)
            webbrowser.open(browser_url)

        # Start browser opener in a daemon thread
        thread = threading.Thread(target=open_browser_after_delay, daemon=True)
        thread.start()

    banner_lines = [
        "<center>██╗  ██╗██╗███╗   ███╗██╗     ██████╗ ██████╗ ██████╗ ███████╗",
        "<center>██║ ██╔╝██║████╗ ████║██║    ██╔════╝██╔═══██╗██╔══██╗██╔════╝",
        "<center>█████╔╝ ██║██╔████╔██║██║    ██║     ██║   ██║██║  ██║█████╗  ",
        "<center>██╔═██╗ ██║██║╚██╔╝██║██║    ██║     ██║   ██║██║  ██║██╔══╝  ",
        "<center>██║  ██╗██║██║ ╚═╝ ██║██║    ╚██████╗╚██████╔╝██████╔╝███████╗",
        "<center>╚═╝  ╚═╝╚═╝╚═╝     ╚═╝╚═╝     ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
        "",
        "<center>WEB UI (Technical Preview)",
        "",
        "<hr>",
        "",
    ]

    # Add URLs for each host (nowrap to keep URLs on single line for easy copying)
    for label, host_addr in display_hosts:
        url, url_with_token = make_url(host_addr)
        if session_token:
            banner_lines.append(f"<nowrap>  ➜  {label:8} {url_with_token}")
        else:
            banner_lines.append(f"<nowrap>  ➜  {label:8} {url}")

    # Auth token or warnings
    if session_token:
        banner_lines.extend(
            [
                "",
                f"<nowrap>  Token:   {session_token}",
            ]
        )
    elif public_mode:
        banner_lines.extend(
            [
                "",
                "<nowrap>  ⚠ AUTH DISABLED - Anyone on the network can access",
            ]
        )

    if restrict_sensitive_apis:
        banner_lines.append("<nowrap>  ⚠ Sensitive APIs are restricted")

    # Show network access mode and tips
    banner_lines.append("")
    banner_lines.append("<hr>")
    banner_lines.append("")

    if not public_mode:
        # Local-only mode (127.0.0.1)
        banner_lines.extend(
            [
                "<nowrap>  Tips:",
                "<nowrap>    • Use -n / --network to share on LAN",
                "<nowrap>    • Use --network --public for public access",
            ]
        )
    elif lan_only:
        # LAN mode (0.0.0.0 with lan_only)
        banner_lines.extend(
            [
                "<nowrap>  Mode: LAN only (private IPs)",
                "",
                "<nowrap>  Tips:",
                "<nowrap>    • Use --public to allow public access",
                "<nowrap>    • ⚠ Public mode allows access from any IP",
            ]
        )
    else:
        # Public mode (0.0.0.0 without lan_only)
        banner_lines.extend(
            [
                "<nowrap>  ⚠ Mode: PUBLIC (all networks)",
                "<nowrap>    Anyone with the URL can access this instance",
                "",
                "<nowrap>  Security tips:",
                "<nowrap>    • Keep your auth token secure",
                "<nowrap>    • Consider using firewall or VPN",
            ]
        )

    banner_lines.append("")

    print_banner(banner_lines)
    # print(f"API docs available at {url}/docs")

    uvicorn.run(
        "kimi_cli.web.app:create_app",
        factory=True,
        host=host,
        port=actual_port,
        reload=reload,
        log_level="info",
        timeout_graceful_shutdown=3,
    )


__all__ = ["create_app", "run_web_server"]
