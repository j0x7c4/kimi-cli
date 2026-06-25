"""Thin signed REST client for CCI 2.0 (spec §1.1/§3).

# hechun-fork-cci

Covers the real CCI 2.0 API surface (spec §1.1):

- pod CRUD under api group **cci/v2**:
  ``/apis/cci/v2/namespaces/{ns}/pods[/{name}]``
- network reads under api group **yangtse/v2**:
  ``/apis/yangtse/v2/namespaces/{ns}/networks/{name}``

Every call is AK/SK-signed via :class:`~kimi_cli.web.spawner.cci_auth.HuaweiSigner`
(spec §1.2: all REST uses AK/SK, no token). ``httpx`` does the transport; imported
lazily so the docker path doesn't need it.

CCI api errors are surfaced as :class:`CciApiError` carrying the HTTP status +
华为 error code so the gateway ``/metrics`` (C-7) can label
``kimo_cci_api_error_total{operation,code}`` (spec §7.2).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from kimi_cli.web.metrics import MetricsState
    from kimi_cli.web.spawner.cci_auth import HuaweiSigner


class CciApiError(RuntimeError):
    """A CCI REST call returned a non-2xx status (spec §7.2 error labels)."""

    def __init__(self, operation: str, status: int, code: str | None, message: str):
        super().__init__(f"CCI {operation} failed: HTTP {status} code={code} {message}")
        self.operation = operation
        self.status = status
        self.code = code or str(status)


class CciRestClient:
    """Thin httpx + HuaweiSigner wrapper over cci/v2 + yangtse/v2 CRUD (spec §3).

    ``endpoint`` is the bare host ``cci.{region}.myhuaweicloud.com`` (no scheme);
    all calls go over HTTPS.
    """

    def __init__(self, endpoint: str, signer: HuaweiSigner, *, timeout: float = 30.0):
        self._base = f"https://{endpoint}"
        self._signer = signer
        self._timeout = timeout
        # Optional metrics sink (spec §7.2 kimo_cci_api_error_total). Backfilled by
        # app.py after MetricsState is built (None on docker path / metrics off →
        # error instrumentation is a silent no-op). NEVER affects request behaviour.
        self.metrics: MetricsState | None = None

    # ── cci/v2 : pods ─────────────────────────────────────────────────────────

    async def create_pod(self, ns: str, pod: dict[str, Any]) -> dict[str, Any]:
        path = f"/apis/cci/v2/namespaces/{quote(ns)}/pods"
        return await self._request("create_pod", "POST", path, body=pod)

    async def read_pod(self, ns: str, name: str) -> dict[str, Any]:
        path = f"/apis/cci/v2/namespaces/{quote(ns)}/pods/{quote(name)}"
        return await self._request("read_pod", "GET", path)

    async def delete_pod(self, ns: str, name: str, grace: int = 10) -> None:
        path = (
            f"/apis/cci/v2/namespaces/{quote(ns)}/pods/{quote(name)}"
            f"?gracePeriodSeconds={int(grace)}"
        )
        await self._request("delete_pod", "DELETE", path)

    async def list_pods(self, ns: str, label_selector: str | None = None) -> list[dict[str, Any]]:
        path = f"/apis/cci/v2/namespaces/{quote(ns)}/pods"
        if label_selector:
            path += f"?labelSelector={quote(label_selector)}"
        resp = await self._request("list_pods", "GET", path)
        items = resp.get("items")
        return list(items) if isinstance(items, list) else []

    # ── cci/v2 : namespaces (bootstrap; C-5) ───────────────────────────────────

    async def create_namespace(self, namespace: dict[str, Any]) -> dict[str, Any]:
        path = "/apis/cci/v2/namespaces"
        return await self._request("create_namespace", "POST", path, body=namespace)

    async def read_namespace(self, name: str) -> dict[str, Any]:
        path = f"/apis/cci/v2/namespaces/{quote(name)}"
        return await self._request("read_namespace", "GET", path)

    # ── yangtse/v2 : networks ──────────────────────────────────────────────────

    async def create_network(self, ns: str, network: dict[str, Any]) -> dict[str, Any]:
        path = f"/apis/yangtse/v2/namespaces/{quote(ns)}/networks"
        return await self._request("create_network", "POST", path, body=network)

    async def read_network(self, ns: str, name: str) -> dict[str, Any]:
        path = f"/apis/yangtse/v2/namespaces/{quote(ns)}/networks/{quote(name)}"
        return await self._request("read_network", "GET", path)

    # ── internals ──────────────────────────────────────────────────────────────

    async def _request(
        self, operation: str, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import httpx  # noqa: PLC0415

        url = f"{self._base}{path}"
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        signed = self._signer.sign(method, url, headers, raw)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method, url, content=raw if raw else None, headers=signed
            )
        return self._parse(operation, resp)

    def _parse(self, operation: str, resp: Any) -> dict[str, Any]:
        status = resp.status_code
        text = resp.text or ""
        parsed: dict[str, Any]
        try:
            parsed = json.loads(text) if text else {}
        except ValueError:
            parsed = {}
        if status >= 400:
            # 华为 k8s-style error body: {"code": "...", "message": "..."} or
            # {"reason": "...", "message": "..."}.
            code = None
            message = text
            if isinstance(parsed, dict):
                code = parsed.get("code") or parsed.get("reason")
                message = parsed.get("message") or text
            err = CciApiError(operation, status, code, message)
            # spec §7.2: emit kimo_cci_api_error_total{operation,code} at the single
            # raise point. ``err.operation`` / ``err.code`` are the labelled values
            # (code falls back to str(status) when 华为 body carries no code/reason).
            if self.metrics is not None:
                self.metrics.record_cci_api_error(
                    operation=err.operation, code=err.code
                )
            raise err
        return parsed if isinstance(parsed, dict) else {}


__all__ = ["CciRestClient", "CciApiError"]
