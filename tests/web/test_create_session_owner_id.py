"""``POST /api/sessions`` ``owner_id`` resolution tests (task B3, spec §2.4.2.D).

Three-priority resolution for ``state.owner_id``:

    ① cookie current_user > ② body.owner_id (service-account) > ③ None (anonymous)

Cookie current_user wins over a malicious ``body.owner_id`` to keep the
webui multi-user surface secure; service-account callers (backend → kimo,
no cookie) fall through to ``body.owner_id``; unauthenticated callers stay
ownerless and land under the ``__anonymous__`` sentinel inside storage.

Persistence happens through the configured :class:`KimoStorage` (file or
postgres) — these tests exercise the FileKimoStorage path end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import Request
from kaos.path import KaosPath

from kimi_cli.session_state import load_session_state
from kimi_cli.web.api import sessions as sessions_api
from kimi_cli.web.api.sessions import CreateSessionRequest


@pytest.fixture
def isolated_share_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    share_dir = tmp_path / "share"
    share_dir.mkdir()

    def _get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("kimi_cli.share.get_share_dir", _get_share_dir)
    monkeypatch.setattr("kimi_cli.metadata.get_share_dir", _get_share_dir)
    return share_dir


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    p = tmp_path / "work"
    p.mkdir()
    return p


def _make_request(
    *,
    current_user: dict | None,
    monkeypatch: pytest.MonkeyPatch,
    kimo_storage,
) -> Request:
    """Build a minimal Request stub for create_session.

    We bypass ``Request.__init__`` (which needs an ASGI scope) by using
    ``SimpleNamespace`` and patch the module-local ``get_current_user``
    import inside ``sessions_api.create_session``.
    """
    fake_state = SimpleNamespace(kimo_storage=kimo_storage)
    fake_app = SimpleNamespace(state=fake_state)
    fake_req = SimpleNamespace(app=fake_app, cookies={}, headers={})

    def _fake_get_current_user(_req):
        return current_user

    # Patch the symbol that ``create_session`` imports inline
    monkeypatch.setattr(
        "kimi_cli.web.user_auth.get_current_user", _fake_get_current_user
    )
    return fake_req  # type: ignore[return-value]


@pytest.fixture
def file_storage(isolated_share_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """FileKimoStorage wired against the isolated share dir."""
    monkeypatch.setenv("KIMI_SHARE_DIR", str(isolated_share_dir))
    monkeypatch.setenv("KIMI_SHARE_HOME", str(isolated_share_dir))
    from kimi_cli.storage.file_storage import FileKimoStorage

    return FileKimoStorage()


@pytest.mark.anyio
class TestCreateSessionOwnerIdPriority:
    """The three-priority resolution rule must hold under all combinations."""

    async def test_cookie_current_user_wins_over_body_owner_id(
        self,
        isolated_share_dir: Path,
        work_dir: Path,
        file_storage,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """① cookie current_user > ② body.owner_id (body must NOT override cookie)."""
        req = _make_request(
            current_user={"id": "webui-cookie-owner", "role": "user"},
            monkeypatch=monkeypatch,
            kimo_storage=file_storage,
        )
        body = CreateSessionRequest(
            work_dir=str(work_dir),
            owner_id="hechun-999",  # adversarial — should be IGNORED
        )

        session = await sessions_api.create_session(
            http_request=req,  # type: ignore[arg-type]
            request=body,
        )

        state = load_session_state(Path(session.session_dir))
        assert state.owner_id == "webui-cookie-owner"

    async def test_body_owner_id_used_when_no_cookie(
        self,
        isolated_share_dir: Path,
        work_dir: Path,
        file_storage,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """② body.owner_id wins when no cookie current_user (service-account path)."""
        req = _make_request(
            current_user=None,
            monkeypatch=monkeypatch,
            kimo_storage=file_storage,
        )
        body = CreateSessionRequest(
            work_dir=str(work_dir),
            owner_id="hechun-42",
        )

        session = await sessions_api.create_session(
            http_request=req,  # type: ignore[arg-type]
            request=body,
        )
        state = load_session_state(Path(session.session_dir))
        assert state.owner_id == "hechun-42"

    async def test_anonymous_fallback_when_neither(
        self,
        isolated_share_dir: Path,
        work_dir: Path,
        file_storage,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """③ No cookie + no body.owner_id → owner_id remains None (anonymous)."""
        req = _make_request(
            current_user=None,
            monkeypatch=monkeypatch,
            kimo_storage=file_storage,
        )
        body = CreateSessionRequest(work_dir=str(work_dir))  # owner_id=None

        session = await sessions_api.create_session(
            http_request=req,  # type: ignore[arg-type]
            request=body,
        )
        # Sessions created without an owner stay ownerless on disk; storage
        # backends (file) keep state.owner_id=None, which routes memory under
        # ``__anonymous__`` at archivist time. State file may not even exist
        # if no owner-tied save happened — but a fresh load returns owner_id=None.
        state = load_session_state(Path(session.session_dir))
        assert state.owner_id is None


@pytest.mark.anyio
class TestCreateSessionStorageBackend:
    """``create_session`` must route the owner_id-tagged state save through
    the configured :class:`KimoStorage` backend (file or pg), not bypass it.
    """

    async def test_save_goes_through_storage_when_configured(
        self,
        isolated_share_dir: Path,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When ``app.state.kimo_storage`` is wired, save_session_state_via_storage
        is the path taken — not the direct Path helper."""
        calls: list[tuple[UUID, str | None]] = []

        class _SpyStorage:
            def save_session_state(self, kimo_session_id, owner_id, state):
                calls.append((kimo_session_id, owner_id))
                # also persist so subsequent load_session_state sees the owner_id
                from kimi_cli.session_state import save_session_state as _save
                from kimi_cli.web.store.sessions import load_session_by_id

                joint = load_session_by_id(kimo_session_id)
                if joint is not None:
                    _save(state, Path(joint.session_dir))

            def load_session_state(self, _sid):
                return None

            def delete_session_state(self, _sid):
                pass

            def append_user_memory(self, _oid, _e):
                pass

            def list_user_memory(self, _oid, limit=200):
                return []

        spy = _SpyStorage()
        req = _make_request(
            current_user={"id": "webui-alice", "role": "user"},
            monkeypatch=monkeypatch,
            kimo_storage=spy,
        )
        body = CreateSessionRequest(work_dir=str(work_dir))
        session = await sessions_api.create_session(
            http_request=req,  # type: ignore[arg-type]
            request=body,
        )

        assert len(calls) == 1
        sid, oid = calls[0]
        assert str(sid) == session.session_id.hex or str(sid) == str(session.session_id)
        assert oid == "webui-alice"


class TestCreateSessionRequestSchema:
    """The new ``owner_id: str | None`` field accepts None + str + omitted."""

    def test_owner_id_defaults_to_none(self):
        req = CreateSessionRequest()
        assert req.owner_id is None

    def test_owner_id_accepts_hechun_prefix(self):
        req = CreateSessionRequest(owner_id="hechun-1234")
        assert req.owner_id == "hechun-1234"

    def test_owner_id_accepts_webui_prefix(self):
        req = CreateSessionRequest(owner_id="webui-abcdef")
        assert req.owner_id == "webui-abcdef"
