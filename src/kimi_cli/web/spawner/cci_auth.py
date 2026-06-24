"""CCI 2.0 authentication: AK/SK request signing + IAM token provider (spec §1.2/§2).

# hechun-fork-cci

Mixed auth (spec §1.2):

- **AK/SK signature** (:class:`HuaweiSigner`) — per-request signing, used for ALL
  REST CRUD (namespace / network / pod). No refresh loop ⇒ no "forgot to refresh
  token, spawn全挂" failure mode.
- **IAM token** (:class:`TokenProvider`) — ``X-Auth-Token`` header, used ONLY for
  the exec WebSocket handshake. Cached 22h (spec leaves a 2h safety margin under
  the 24h validity). token 过期对我们无感: 普通 REST 不用 token; 单条 exec 流
  鉴权只在握手那一刻校验, 之后 stream 存活期不再回查 (spec §1.2).

Deps: ``huaweicloudsdkcore`` (Signer) + ``httpx`` (IAM getToken). Both are imported
lazily so the module loads under the docker spawner path without these installed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# IAM endpoint template. Region is interpolated at construction (spec §1.2: IAM
# v3 getToken). Kept module-level so tests can monkeypatch it.
IAM_ENDPOINT_TEMPLATE = "https://iam.{region}.myhuaweicloud.com"
IAM_TOKEN_PATH = "/v3/auth/tokens"

# token cached 22h, not the full 24h validity — 2h safety margin (spec §1.2/§2).
_TOKEN_TTL = timedelta(hours=22)


class HuaweiSigner:
    """AK/SK per-request signer for all REST calls (spec §2).

    Wraps ``huaweicloudsdkcore.signer.Signer``. The华为 SDK signs a mutable
    request object in place, adding ``Authorization`` / ``X-Sdk-Date`` headers.
    We adapt that to a stateless ``sign(method,url,headers,body) -> headers`` so
    :class:`~kimi_cli.web.spawner.cci_client.CciRestClient` stays a thin httpx
    wrapper.
    """

    def __init__(self, ak: str, sk: str):
        if not ak or not sk:
            raise ValueError("HuaweiSigner requires non-empty ak/sk")
        self._ak = ak
        self._sk = sk

    def sign(
        self, method: str, url: str, headers: dict[str, str], body: bytes
    ) -> dict[str, str]:
        """Return headers augmented with华为 AK/SK signature headers.

        Does not mutate the input ``headers`` dict; returns a new merged dict.
        ``X-Sdk-Date`` + ``Authorization`` are computed by the SDK Signer.

        The华为 ``Signer`` builds its canonical request from ``request.host`` (→
        Host header), ``request.resource_path`` (canonical URI) and
        ``request.query_params`` (canonical query string) — NOT from the raw
        ``uri`` field. So we parse the URL into those parts; passing the full
        URL as ``uri`` alone yields a wrong signature.
        """
        # Lazy import: only needed on the CCI path.
        from urllib.parse import parse_qsl, urlsplit  # noqa: PLC0415

        from huaweicloudsdkcore.auth.credentials import BasicCredentials  # noqa: PLC0415
        from huaweicloudsdkcore.sdk_request import SdkRequest  # noqa: PLC0415
        from huaweicloudsdkcore.signer.signer import Signer  # noqa: PLC0415

        parts = urlsplit(url)
        query_params = parse_qsl(parts.query, keep_blank_values=True)
        merged = dict(headers)
        sdk_request = SdkRequest(
            method=method.upper(),
            schema=parts.scheme or "https",
            host=parts.netloc,
            resource_path=parts.path,
            uri=parts.path,
            query_params=query_params,
            header_params=merged,
            body=body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else (body or ""),
        )
        credentials = BasicCredentials(self._ak, self._sk)
        signer = Signer(credentials)
        signer.sign(sdk_request)
        # SdkRequest.header_params now carries Authorization / X-Sdk-Date (+ Host).
        return dict(sdk_request.header_params)


class TokenProvider:
    """AK/SK → IAM getToken → short-lived ``X-Auth-Token`` for exec handshake.

    Caches the token for 22h and refreshes on demand (spec §2). ``token()`` is
    async because ``_fetch_token`` does network IO via httpx.
    """

    def __init__(self, ak: str, sk: str, region: str, *, domain: str | None = None):
        if not ak or not sk or not region:
            raise ValueError("TokenProvider requires ak/sk/region")
        self._ak = ak
        self._sk = sk
        self._region = region
        # IAM v3 getToken needs the IAM 用户名 + 所属账号(domain). For AK/SK based
        # token issuance华为 also exposes ``securitytoken`` issuance; we use the
        # documented IAM password-less AK/SK path via the SDK credential.
        self._domain = domain
        self._cached: str | None = None
        self._expires_at: datetime | None = None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)

    async def token(self) -> str:
        if self._cached and self._expires_at and self._now() < self._expires_at:
            return self._cached
        self._cached = await self._fetch_token()
        self._expires_at = self._now() + _TOKEN_TTL
        return self._cached

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after a 401 on handshake)."""
        self._cached = None
        self._expires_at = None

    async def _fetch_token(self) -> str:
        """Call IAM v3 getToken signed with AK/SK; return the X-Subject-Token.

        IAM accepts AK/SK-signed requests; the issued token comes back in the
        ``X-Subject-Token`` response header (华为 IAM v3 convention).
        """
        import httpx  # noqa: PLC0415

        endpoint = IAM_ENDPOINT_TEMPLATE.format(region=self._region)
        url = f"{endpoint}{IAM_TOKEN_PATH}"
        # AK/SK-scoped token request body (IAM v3). 仅请求 project-scoped token,
        # 区域即 project name.
        payload = {
            "auth": {
                "identity": {
                    "methods": ["hw_ak_sk"],
                    "hw_ak_sk": {"access": {"key": self._ak}, "secret": {"key": self._sk}},
                },
                "scope": {"project": {"name": self._region}},
            }
        }
        signer = HuaweiSigner(self._ak, self._sk)
        import json  # noqa: PLC0415

        body = json.dumps(payload).encode("utf-8")
        headers = signer.sign(
            "POST", url, {"Content-Type": "application/json"}, body
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=body, headers=headers)
            resp.raise_for_status()
            subject_token = resp.headers.get("X-Subject-Token")
            if not subject_token:
                raise RuntimeError("IAM getToken returned no X-Subject-Token header")
            return subject_token


__all__ = ["HuaweiSigner", "TokenProvider"]
