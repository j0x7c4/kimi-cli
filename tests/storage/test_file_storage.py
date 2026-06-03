"""FileKimoStorage round-trip + smoke tests (spec §2.4.2.C).

Validates that the file backend stays bit-for-bit upstream-compatible: it
writes through the same upstream helpers (load_session_state /
save_session_state / append_entry / read_entries), so behaviour against the
on-disk layout is preserved.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.memory.paths import get_persistent_memory_file
from kimi_cli.session_state import SessionState
from kimi_cli.storage.file_storage import FileKimoStorage


@pytest.fixture
def isolated_share_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``KIMI_SHARE_DIR`` + ``kimi.json`` metadata file under tmp_path."""
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
    # Both metadata.get_metadata_file and memory.paths read KIMI_SHARE_DIR; the
    # former via share.get_share_dir which honours an env override.
    monkeypatch.setenv("KIMI_SHARE_HOME", str(share))
    return share


@pytest.fixture
def storage_with_workdir(
    isolated_share_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FileKimoStorage, Path]:
    """Prepare a FileKimoStorage with one work-dir registered in metadata.

    Returns the storage instance and the resolved sessions_dir for that wd so
    tests can poke at the on-disk layout.
    """
    from kimi_cli.metadata import Metadata, WorkDirMeta, save_metadata
    from kimi_cli.share import get_share_dir

    wd_path = isolated_share_dir / "project"
    wd_path.mkdir()
    wd = WorkDirMeta(path=str(wd_path))
    md = Metadata(work_dirs=[wd])
    save_metadata(md)
    # Touch share_dir to confirm fixture wiring
    assert get_share_dir().exists()
    return FileKimoStorage(), wd.sessions_dir


class TestFileSessionState:
    def test_load_returns_none_when_no_dir(self, storage_with_workdir):
        storage, _ = storage_with_workdir
        # Random unknown UUID → None (no state.json anywhere)
        assert storage.load_session_state(uuid4()) is None

    def test_save_then_load_round_trip(self, storage_with_workdir):
        storage, sessions_dir = storage_with_workdir
        sid = uuid4()
        state = SessionState(custom_title="hechun-test", title_generated=True)
        storage.save_session_state(sid, owner_id="hechun-42", state=state)

        # File landed at sessions_dir/<uuid>/state.json
        state_file = sessions_dir / str(sid) / "state.json"
        assert state_file.exists()

        loaded = storage.load_session_state(sid)
        assert loaded is not None
        assert loaded.custom_title == "hechun-test"
        assert loaded.title_generated is True
        # owner_id forwarded into SessionState
        assert loaded.owner_id == "hechun-42"

    def test_save_preserves_existing_owner_when_called_with_none(
        self, storage_with_workdir
    ):
        storage, _ = storage_with_workdir
        sid = uuid4()
        s1 = SessionState(owner_id="hechun-1", custom_title="x")
        storage.save_session_state(sid, owner_id="hechun-1", state=s1)
        # second save with owner_id=None — current owner_id on the SessionState
        # is kept (we only overwrite when caller supplies a non-None owner).
        s1.custom_title = "x-updated"
        storage.save_session_state(sid, owner_id=None, state=s1)
        loaded = storage.load_session_state(sid)
        assert loaded.custom_title == "x-updated"
        assert loaded.owner_id == "hechun-1"

    def test_delete_removes_session_dir(self, storage_with_workdir):
        storage, sessions_dir = storage_with_workdir
        sid = uuid4()
        storage.save_session_state(sid, "hechun-1", SessionState())
        dirpath = sessions_dir / str(sid)
        assert dirpath.is_dir()
        storage.delete_session_state(sid)
        assert not dirpath.exists()

    def test_delete_is_idempotent_on_missing(self, storage_with_workdir):
        storage, _ = storage_with_workdir
        # No exception when target doesn't exist
        storage.delete_session_state(uuid4())


class TestFileUserMemory:
    def _make_entry(self, content: str) -> MemoryEntry:
        return MemoryEntry(kind="user", scope="persistent", content=content)

    def test_append_then_list(self, storage_with_workdir):
        storage, _ = storage_with_workdir
        owner = "hechun-7"
        e1 = self._make_entry("first thing")
        e2 = self._make_entry("second thing")
        storage.append_user_memory(owner, e1)
        storage.append_user_memory(owner, e2)
        entries = storage.list_user_memory(owner)
        assert [e.content for e in entries] == ["second thing", "first thing"]

    def test_list_respects_limit(self, storage_with_workdir):
        storage, _ = storage_with_workdir
        owner = "hechun-7"
        for i in range(5):
            storage.append_user_memory(owner, self._make_entry(f"m{i}"))
        entries = storage.list_user_memory(owner, limit=2)
        assert len(entries) == 2
        # Most-recent-first
        assert entries[0].content == "m4"
        assert entries[1].content == "m3"

    def test_list_empty_for_unknown_owner(self, storage_with_workdir):
        storage, _ = storage_with_workdir
        assert storage.list_user_memory("hechun-nobody") == []

    def test_append_lands_in_expected_path(self, storage_with_workdir):
        storage, _ = storage_with_workdir
        owner = "hechun-42"
        storage.append_user_memory(owner, self._make_entry("hello"))
        # File path matches upstream get_persistent_memory_file
        expected = get_persistent_memory_file(owner)
        assert expected.exists()
        assert expected.read_text(encoding="utf-8").strip().startswith("{")
