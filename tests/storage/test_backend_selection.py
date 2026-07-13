"""Backend selection: build_storage mysql/cci-no-file + build_spawner (C-9).

# hechun-fork-cci
"""

from __future__ import annotations

import pytest

from kimi_cli.storage import build_storage
from kimi_cli.web.spawner import build_spawner

_MYSQL_VARS = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_PORT", "MYSQL_DB")


def _clear_mysql_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIMO_DB_URL", raising=False)
    monkeypatch.delenv("KIMI_SPAWNER_BACKEND", raising=False)
    for k in _MYSQL_VARS:
        monkeypatch.delenv(k, raising=False)


def _capture_mysql_url(monkeypatch: pytest.MonkeyPatch):
    """Patch MyKimoStorage so build_storage() doesn't open an engine; capture URL."""
    captured: dict = {}

    class _FakeStorage:
        def __init__(self, db_url, pool_size=5):
            captured["db_url"] = db_url
            captured["pool_size"] = pool_size

    monkeypatch.setattr("kimi_cli.storage.my_storage.MyKimoStorage", _FakeStorage)
    return captured


class TestStorageSelection:
    def test_mysql_requires_components_or_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        _clear_mysql_env(monkeypatch)
        with pytest.raises(RuntimeError, match="MYSQL_HOST"):
            build_storage()

    def test_mysql_builds_url_from_components(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        _clear_mysql_env(monkeypatch)
        monkeypatch.setenv("MYSQL_HOST", "rds.internal")
        monkeypatch.setenv("MYSQL_USER", "hechun")
        monkeypatch.setenv("MYSQL_PASSWORD", "secret")
        monkeypatch.setenv("MYSQL_PORT", "4727")
        cap = _capture_mysql_url(monkeypatch)
        build_storage()
        url = cap["db_url"]
        # A URL object (not a string) is built via URL.create.
        from sqlalchemy.engine import URL

        assert isinstance(url, URL)
        assert url.host == "rds.internal"
        assert url.port == 4727
        assert url.username == "hechun"
        assert url.database == "hechun"  # default
        assert url.query.get("charset") == "utf8mb4"

    def test_mysql_password_special_chars_dont_pollute_host(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        _clear_mysql_env(monkeypatch)
        monkeypatch.setenv("MYSQL_HOST", "rds.internal")
        monkeypatch.setenv("MYSQL_USER", "hechun")
        # password with @ : / ! — would corrupt a hand-built DSN.
        monkeypatch.setenv("MYSQL_PASSWORD", "p@ss:w/rd!")
        cap = _capture_mysql_url(monkeypatch)
        build_storage()
        url = cap["db_url"]
        assert url.host == "rds.internal"  # NOT "ss:w" or "rd!@rds..."
        assert url.username == "hechun"
        assert url.password == "p@ss:w/rd!"
        # Rendered DSN escapes the password (no raw @ before the real host @).
        rendered = url.render_as_string(hide_password=False)
        assert "p%40ss%3Aw%2Frd%21@rds.internal" in rendered

    def test_kimo_db_url_override_used_when_no_components(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        _clear_mysql_env(monkeypatch)
        monkeypatch.setenv("KIMO_DB_URL", "mysql+pymysql://u:p@h:3306/hechun?charset=utf8mb4")
        cap = _capture_mysql_url(monkeypatch)
        build_storage()
        assert cap["db_url"] == "mysql+pymysql://u:p@h:3306/hechun?charset=utf8mb4"

    def test_components_preferred_over_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        _clear_mysql_env(monkeypatch)
        monkeypatch.setenv("MYSQL_HOST", "rds.internal")
        monkeypatch.setenv("MYSQL_USER", "hechun")
        monkeypatch.setenv("MYSQL_PASSWORD", "secret")
        monkeypatch.setenv("KIMO_DB_URL", "mysql+pymysql://ignored@nope/hechun")
        cap = _capture_mysql_url(monkeypatch)
        build_storage()
        from sqlalchemy.engine import URL

        # Components win: a URL object built from MYSQL_*, not the raw override.
        assert isinstance(cap["db_url"], URL)
        assert cap["db_url"].host == "rds.internal"

    def test_postgres_still_requires_kimo_db_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "postgres")
        monkeypatch.delenv("KIMO_DB_URL", raising=False)
        monkeypatch.delenv("KIMI_SPAWNER_BACKEND", raising=False)
        with pytest.raises(RuntimeError, match="KIMO_DB_URL"):
            build_storage()

    def test_cci_spawner_forbids_file_storage(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_SPAWNER_BACKEND", "cci")
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "file")
        with pytest.raises(RuntimeError, match="no persistent volume"):
            build_storage()

    def test_file_backend_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_STORAGE_BACKEND", raising=False)
        monkeypatch.delenv("KIMI_SPAWNER_BACKEND", raising=False)
        monkeypatch.delenv("KIMO_MEMORY_VIA_GATEWAY", raising=False)
        storage = build_storage()
        assert type(storage).__name__ == "FileKimoStorage"

    def test_mysql_via_gateway_returns_remote_proxy(self, monkeypatch: pytest.MonkeyPatch):
        """hechun-fork-cci (方案 B): KIMO_MEMORY_VIA_GATEWAY → RemoteKimoStorage.

        The CCI worker cannot reach RDS, so with the flag set build_storage()
        must return the wire-delegating proxy — and must NOT need MYSQL_* creds
        (no DB connection is opened on the worker).
        """
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        monkeypatch.setenv("KIMO_MEMORY_VIA_GATEWAY", "1")
        _clear_mysql_env(monkeypatch)  # no MYSQL_* creds at all
        storage = build_storage()
        assert type(storage).__name__ == "RemoteKimoStorage"

    def test_mysql_without_gateway_flag_builds_real_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Docker/SIT (flag unset) keeps the direct MyKimoStorage path."""
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        monkeypatch.delenv("KIMO_MEMORY_VIA_GATEWAY", raising=False)
        _clear_mysql_env(monkeypatch)
        monkeypatch.setenv("MYSQL_HOST", "rds.internal")
        monkeypatch.setenv("MYSQL_USER", "hechun")
        monkeypatch.setenv("MYSQL_PASSWORD", "secret")
        cap = _capture_mysql_url(monkeypatch)
        storage = build_storage()
        assert type(storage).__name__ == "_FakeStorage"
        assert cap["db_url"].host == "rds.internal"


class TestSpawnerSelection:
    def test_docker_default_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_SPAWNER_BACKEND", raising=False)
        assert build_spawner() is None

    def test_unknown_backend_falls_back_to_docker_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_SPAWNER_BACKEND", "bogus")
        assert build_spawner() is None

    def test_cci_requires_credentials(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_SPAWNER_BACKEND", "cci")
        for k in ("HUAWEICLOUD_AK", "HUAWEICLOUD_SK", "HUAWEICLOUD_CCI_REGION"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(RuntimeError):
            build_spawner()
