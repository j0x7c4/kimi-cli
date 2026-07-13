"""方案 B deployment wiring: KIMO_MEMORY_VIA_GATEWAY injection + no MYSQL_* leak.

# hechun-fork-cci

Guards the three load-bearing invariants of the memory-over-gateway wiring:

1. The sandbox env (docker cmd + CCI env dict) carries ``KIMO_MEMORY_VIA_GATEWAY=1``
   in DB mode, so the worker's ``build_storage()`` picks ``RemoteKimoStorage``.
2. The sandbox env NEVER carries the main-DB credentials (MYSQL_* / KIMO_DB_URL) —
   under 方案 B the worker delegates to the gateway and must not hold RDS creds.
3. The GATEWAY's own ``build_storage()`` is unaffected: it must return a real
   ``MyKimoStorage`` (RDS) because the flag lives only in the sandbox env, never in
   the gateway process env — otherwise the gateway would dead-end forwarding wire
   frames to a non-existent upstream.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kimi_cli.web.runner.cci_process import CCISessionProcess
from kimi_cli.web.runner.container import (
    _SANDBOX_ENV_VARS,
    ContainerSessionProcess,
    _memory_via_gateway_flag,
)

_DB_CRED_VARS = (
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_DB",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "KIMO_DB_URL",
    "KIMO_DB_POOL_SIZE",
)


class TestFlagComputation:
    def test_mysql_backend_yields_flag(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        assert _memory_via_gateway_flag() == "1"

    def test_postgres_backend_yields_flag(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "postgres")
        assert _memory_via_gateway_flag() == "1"

    def test_file_mode_yields_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "file")
        assert _memory_via_gateway_flag() is None

    def test_unset_backend_yields_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_STORAGE_BACKEND", raising=False)
        assert _memory_via_gateway_flag() is None


class TestSandboxEnvNeverCarriesDbCreds:
    def test_db_creds_removed_from_passthrough_whitelist(self):
        """MYSQL_* / KIMO_DB_URL must NOT be in the host→sandbox passthrough set."""
        for var in _DB_CRED_VARS:
            assert var not in _SANDBOX_ENV_VARS, f"{var} must not leak into sandbox"

    def test_storage_backend_still_forwarded(self):
        """Worker still needs KIMI_STORAGE_BACKEND to know it is in DB (vs file) mode."""
        assert "KIMI_STORAGE_BACKEND" in _SANDBOX_ENV_VARS


class TestDockerCmdInjection:
    def _cmd(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        # _build_docker_cmd hits _read_owner_id_from_disk → build_storage(); stub it.
        monkeypatch.setattr(
            "kimi_cli.web.runner.container._read_owner_id_from_disk", lambda _sid: None
        )
        monkeypatch.setattr(
            "kimi_cli.web.runner.container._read_subagent_from_disk", lambda _sid: None
        )
        proc = ContainerSessionProcess(uuid4(), image="img:test")
        return proc._build_docker_cmd()

    def _env_from_cmd(self, cmd: list[str]) -> dict[str, str]:
        env: dict[str, str] = {}
        it = iter(cmd)
        for tok in it:
            if tok == "-e":
                kv = next(it)
                k, _, v = kv.partition("=")
                env[k] = v
        return env

    def test_flag_injected_in_db_mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        # Ensure no DB creds are in the gateway env → prove they aren't forwarded
        # even when present.
        monkeypatch.setenv("MYSQL_HOST", "rds.internal")
        monkeypatch.setenv("MYSQL_PASSWORD", "secret")
        monkeypatch.setenv("KIMO_DB_URL", "mysql+pymysql://u:p@h/hechun")
        env = self._env_from_cmd(self._cmd(monkeypatch))
        assert env.get("KIMO_MEMORY_VIA_GATEWAY") == "1"
        assert env.get("KIMI_STORAGE_BACKEND") == "mysql"
        for var in _DB_CRED_VARS:
            assert var not in env, f"{var} leaked into docker sandbox env"

    def test_flag_absent_in_file_mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "file")
        env = self._env_from_cmd(self._cmd(monkeypatch))
        assert "KIMO_MEMORY_VIA_GATEWAY" not in env


class TestCciEnvInjection:
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
        monkeypatch.setattr(
            "kimi_cli.web.runner.cci_process._read_owner_id_from_disk", lambda _sid: None
        )
        monkeypatch.setattr(
            "kimi_cli.web.runner.cci_process._read_agent_name_from_disk", lambda _sid: None
        )

        class _FakeSpawner:
            pass

        proc = CCISessionProcess(uuid4(), spawner=_FakeSpawner())  # type: ignore[arg-type]
        return proc._build_sandbox_env()

    def test_flag_injected_in_db_mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        monkeypatch.setenv("MYSQL_HOST", "rds.internal")
        monkeypatch.setenv("MYSQL_PASSWORD", "secret")
        env = self._env(monkeypatch)
        assert env.get("KIMO_MEMORY_VIA_GATEWAY") == "1"
        assert env.get("KIMI_STORAGE_BACKEND") == "mysql"
        for var in _DB_CRED_VARS:
            assert var not in env, f"{var} leaked into CCI sandbox env"

    def test_flag_absent_in_file_mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "file")
        env = self._env(monkeypatch)
        assert "KIMO_MEMORY_VIA_GATEWAY" not in env


class TestGatewayBuildStorageUnaffected:
    """The invariant that makes the whole scheme work: the gateway process env has
    no KIMO_MEMORY_VIA_GATEWAY, so its build_storage() returns a real MyKimoStorage,
    NOT a RemoteKimoStorage. We simulate the gateway process env (creds present,
    flag ABSENT) and assert the factory picks MyKimoStorage.
    """

    def test_gateway_env_without_flag_builds_real_mysql_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from kimi_cli.storage import build_storage

        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        # Gateway process env: has RDS creds, does NOT have the sandbox-only flag.
        monkeypatch.delenv("KIMO_MEMORY_VIA_GATEWAY", raising=False)
        monkeypatch.setenv("MYSQL_HOST", "rds.internal")
        monkeypatch.setenv("MYSQL_USER", "hechun")
        monkeypatch.setenv("MYSQL_PASSWORD", "secret")

        captured: dict = {}

        class _FakeMyStorage:
            def __init__(self, db_url, pool_size=5):
                captured["built"] = True

        monkeypatch.setattr(
            "kimi_cli.storage.my_storage.MyKimoStorage", _FakeMyStorage
        )
        storage = build_storage()
        assert captured.get("built") is True
        assert type(storage).__name__ == "_FakeMyStorage"

    def test_flag_present_would_flip_to_remote(self, monkeypatch: pytest.MonkeyPatch):
        """Sanity mirror: WITH the flag (i.e. the sandbox/worker env), the same
        backend yields RemoteKimoStorage — proving the flag is the discriminator."""
        from kimi_cli.storage import build_storage

        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        monkeypatch.setenv("KIMO_MEMORY_VIA_GATEWAY", "1")
        # No creds needed on this path.
        for var in _DB_CRED_VARS:
            monkeypatch.delenv(var, raising=False)
        storage = build_storage()
        assert type(storage).__name__ == "RemoteKimoStorage"
