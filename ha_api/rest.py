"""HA 的 HTTP 部分：探测、读状态、调服务。

只用 REST 能完成控制闭环；区域名要 WS，见 `ws.py`。

URL 拼接注意：`base_url` 允许带路径前缀（反代挂在 `/ha/` 下很常见），所以
所有请求用**不带前导斜杠**的相对路径，交给 httpx 的 urljoin 语义处理。
写成 `"/api/"` 会把反代前缀吞掉。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .exceptions import HaAuthError, HaConnectionError, HaError, HaServiceError

# 用户从浏览器地址栏直接复制时会带上的前端路径。这些段及其后所有内容都不属于
# 反代前缀，必须剥掉——否则 `http://h:8123/lovelace/0` 会拼成
# `http://h:8123/lovelace/0/api/`。
_WEB_PATH_RE = re.compile(r"/(?:lovelace|config|history|logbook|developer-tools|api|profile|calendar)(?:/.*)?$")


def normalize_base_url(raw: str) -> str:
    """把用户输入的地址规整成 ``scheme://host[:port][/prefix]/``。

    允许的输入：``homeassistant.local:8123``、``http://192.168.1.10:8123/``、
    ``https://ha.example.com/ha``、``http://h:8123/lovelace/0``。
    空值返回空串，由调用方报"未配置"。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"

    parts = urlsplit(text)
    path = _WEB_PATH_RE.sub("", parts.path.rstrip("/")).rstrip("/")

    return urlunsplit((parts.scheme, parts.netloc, f"{path}/" if path else "/", "", ""))


class AsyncHaRest:
    """一个实例对应一个 HA 实例、一个令牌。纯异步，禁止同步 IO。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self._token = (token or "").strip()
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    # ── 底层 ────────────────────────────────────────────────────────────
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HaConnectionError(f"无法连接 Home Assistant（{self.base_url}）：{exc}") from exc

        if response.status_code in (401, 403):
            raise HaAuthError(
                "令牌无效、已过期，或该用户没有此操作权限。请到 HA 的「个人资料 → 安全」重新生成长期访问令牌。"
            )
        if response.status_code >= 400:
            raise HaServiceError(
                f"Home Assistant 返回 {response.status_code}：{response.text[:200]}",
                detail={"status": response.status_code},
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # ── 探测 ────────────────────────────────────────────────────────────
    async def ping(self) -> dict[str, Any]:
        """`GET /api/`：确认地址与令牌都可用。"""
        payload = await self._request("GET", "api/")
        if not isinstance(payload, dict) or "message" not in payload:
            raise HaError(f"该地址看起来不是 Home Assistant 的 API：{self.base_url}")
        return payload

    async def get_config(self) -> dict[str, Any]:
        """`GET /api/config`：版本、时区、温度单位。"""
        payload = await self._request("GET", "api/config")
        return payload if isinstance(payload, dict) else {}

    # ── 状态 ────────────────────────────────────────────────────────────
    async def get_states(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "api/states")
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            payload = await self._request("GET", f"api/states/{entity_id}")
        except HaServiceError as exc:
            # 实体不存在就是 404；这是一个正常结果，不是故障。
            if (exc.detail or {}).get("status") == 404:
                return None
            raise
        return payload if isinstance(payload, dict) else None

    # ── 服务调用 ────────────────────────────────────────────────────────
    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        *,
        return_response: bool = False,
    ) -> Any:
        """`POST /api/services/<domain>/<service>`，返回 HA 的 changed states。"""
        url = f"api/services/{domain}/{service}"
        if return_response:
            url += "?return_response"
        try:
            return await self._request("POST", url, json=data or {})
        except HaServiceError as exc:
            raise HaServiceError(
                f"调用 {domain}.{service} 失败：{exc}",
                detail=exc.detail,
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncHaRest:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
