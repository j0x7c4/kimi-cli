"""KimoStorage Protocol contract + build_storage factory tests.

These tests verify the Protocol surface is honoured by every implementation
and that the env-driven factory wiring works without touching real Postgres.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from kimi_cli.storage import KimoStorage, build_storage
from kimi_cli.storage.file_storage import FileKimoStorage


class TestProtocolConformance:
    def test_file_storage_satisfies_protocol(self):
        # runtime structural check (Protocol is non-runtime-checkable by default,
        # but isinstance on subclass-like checks are unnecessary; rely on the
        # presence of all required methods with the right arity instead).
        impl = FileKimoStorage()
        for name, expected_args in [
            ("load_session_state", 1),
            ("save_session_state", 3),
            ("delete_session_state", 1),
            ("append_user_memory", 2),
            ("list_user_memory", 1),  # owner_id required; limit has default
        ]:
            method = getattr(impl, name)
            assert callable(method), f"{name} missing"
            sig = inspect.signature(method)
            required = [
                p
                for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)
            ]
            assert len(required) == expected_args, (
                f"{name} expected {expected_args} required args, got {len(required)}"
            )

    def test_pg_storage_module_imports_clean(self):
        # We don't instantiate (no live pg URL); just ensure the module imports
        # and exposes the class. ORM is built lazily inside __init__.
        from kimi_cli.storage import pg_storage

        assert hasattr(pg_storage, "PgKimoStorage")


class TestBuildStorageFactory:
    def test_default_is_file_backend(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_STORAGE_BACKEND", raising=False)
        storage = build_storage()
        assert isinstance(storage, FileKimoStorage)

    def test_explicit_file_backend(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "file")
        storage = build_storage()
        assert isinstance(storage, FileKimoStorage)

    def test_unknown_backend_falls_back_to_file(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "etcd")  # nonsense
        storage = build_storage()
        # Spec §2.4.2.C: file is the safe default; unknown values must NOT crash.
        assert isinstance(storage, FileKimoStorage)

    def test_postgres_without_db_url_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "postgres")
        monkeypatch.delenv("KIMO_DB_URL", raising=False)
        with pytest.raises(RuntimeError, match="KIMO_DB_URL"):
            build_storage()

    def test_postgres_invalid_pool_size_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("KIMO_DB_URL", "postgresql://x@127.0.0.1/x")
        monkeypatch.setenv("KIMO_DB_POOL_SIZE", "not-a-number")
        with pytest.raises(RuntimeError, match="KIMO_DB_POOL_SIZE"):
            build_storage()

    def test_protocol_type_visible(self):
        # The Protocol is importable; helps downstream task ⑥/⑦/⑧ annotate.
        assert KimoStorage is not None
