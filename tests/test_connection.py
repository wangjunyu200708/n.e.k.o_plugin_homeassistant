"""REST 客户端、URL 规整与连接信息。"""

import httpx
import pytest
from ha_api.client import ConnectionInfo
from ha_api.exceptions import HaAuthError, HaConnectionError, HaServiceError
from ha_api.rest import AsyncHaRest, normalize_base_url


class TestNormalizeBaseUrl:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ""),
            ("homeassistant.local:8123", "http://homeassistant.local:8123/"),
            ("http://192.168.1.10:8123", "http://192.168.1.10:8123/"),
            ("http://192.168.1.10:8123/", "http://192.168.1.10:8123/"),
            ("https://ha.example.com/ha", "https://ha.example.com/ha/"),
            ("http://h:8123/lovelace/0", "http://h:8123/"),
            ("http://h:8123/api/", "http://h:8123/"),
            ("http://h:8123/config", "http://h:8123/"),
            ("  http://h:8123  ", "http://h:8123/"),
        ],
    )
    def test_cases(self, raw, expected):
        assert normalize_base_url(raw) == expected


class TestConnectionInfo:
    def test_configured(self):
        assert ConnectionInfo().configured is False
        assert ConnectionInfo(base_url="http://h:8123/").configured is False
        assert ConnectionInfo(base_url="http://h:8123/", token="t").configured is True

    def test_roundtrip(self):
        original = ConnectionInfo(base_url="http://h:8123/", token="secret-token", ha_version="2026.10.0")
        restored = ConnectionInfo.from_dict(original.to_dict())
        assert restored == original

    def test_from_dict_garbage(self):
        assert ConnectionInfo.from_dict(None) == ConnectionInfo()
        assert ConnectionInfo.from_dict("x") == ConnectionInfo()

    def test_public_hides_token(self):
        info = ConnectionInfo(base_url="http://h:8123/", token="abcdefgh")
        public = info.public()
        assert "token" not in public
        assert public["token_tail"] == "****efgh"

    def test_public_short_token(self):
        assert ConnectionInfo(token="ab").public()["token_tail"] == ""


def _client_with(handler) -> AsyncHaRest:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://ha.local:8123/")
    return AsyncHaRest("http://ha.local:8123", "tok", client=client)


class TestRest:
    async def test_ping_ok(self):
        rest = _client_with(lambda request: httpx.Response(200, json={"message": "API running."}))
        assert await rest.ping() == {"message": "API running."}

    async def test_ping_path(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"message": "API running."})

        rest = _client_with(handler)
        await rest.ping()
        assert seen["path"] == "/api/"
        # 传了外部 client 时 headers 由调用方负责；这里只验证路径拼接正确
        assert "/api/" in seen["path"]

    async def test_401_raises_auth_error(self):
        rest = _client_with(lambda request: httpx.Response(401, text="unauthorized"))
        with pytest.raises(HaAuthError):
            await rest.ping()

    async def test_403_raises_auth_error(self):
        rest = _client_with(lambda request: httpx.Response(403, text="forbidden"))
        with pytest.raises(HaAuthError):
            await rest.get_states()

    async def test_500_raises_service_error(self):
        rest = _client_with(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(HaServiceError):
            await rest.call_service("light", "turn_on", {"entity_id": "light.a"})

    async def test_connect_error(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        rest = _client_with(handler)
        with pytest.raises(HaConnectionError):
            await rest.ping()

    async def test_get_states_filters_non_dict(self):
        rest = _client_with(lambda request: httpx.Response(200, json=[{"entity_id": "light.a"}, "junk"]))
        states = await rest.get_states()
        assert len(states) == 1

    async def test_get_state_404_returns_none(self):
        rest = _client_with(lambda request: httpx.Response(404, json={"message": "not found"}))
        assert await rest.get_state("light.missing") is None

    async def test_call_service_body(self):
        import json

        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=[])

        rest = _client_with(handler)
        await rest.call_service("light", "turn_on", {"entity_id": "light.a", "brightness_pct": 50})
        assert captured["path"] == "/api/services/light/turn_on"
        assert captured["body"]["brightness_pct"] == 50
        assert captured["body"]["entity_id"] == "light.a"

    async def test_service_error_wrapped(self):
        rest = _client_with(lambda request: httpx.Response(400, text="bad"))
        with pytest.raises(HaServiceError) as exc:
            await rest.call_service("light", "turn_on", {})
        assert "light.turn_on" in str(exc.value)


class TestDefaultBaseUrl:
    """地址留空时兜底到本机 8123——面板预填的就是它。"""

    async def test_test_connection_uses_default_when_empty(self, plugin_module, monkeypatch):
        seen = {}

        async def fake_probe(base_url, token, **_kwargs):
            seen["base_url"] = base_url
            raise plugin_module.HaConnectionError("stop here")

        monkeypatch.setattr(plugin_module, "probe_connection", fake_probe)
        plugin = plugin_module.HomeAssistantPlugin(None)
        out = await plugin.test_connection(base_url="", token="tok")
        assert seen["base_url"] == plugin_module.DEFAULT_BASE_URL
        assert not out.is_ok()

    async def test_save_connection_uses_default_when_blank(self, plugin_module, monkeypatch):
        seen = {}

        async def fake_probe(base_url, token, **_kwargs):
            seen["base_url"] = base_url
            raise plugin_module.HaConnectionError("stop here")

        monkeypatch.setattr(plugin_module, "probe_connection", fake_probe)
        plugin = plugin_module.HomeAssistantPlugin(None)
        await plugin.save_connection(base_url="   ", token="tok")
        assert seen["base_url"] == plugin_module.DEFAULT_BASE_URL

    async def test_token_still_required(self, plugin_module):
        plugin = plugin_module.HomeAssistantPlugin(None)
        out = await plugin.test_connection(base_url="http://h:8123", token="")
        assert not out.is_ok()
        assert out.error.code == "missing_parameter"

    async def test_explicit_url_wins(self, plugin_module, monkeypatch):
        seen = {}

        async def fake_probe(base_url, token, **_kwargs):
            seen["base_url"] = base_url
            raise plugin_module.HaConnectionError("stop here")

        monkeypatch.setattr(plugin_module, "probe_connection", fake_probe)
        plugin = plugin_module.HomeAssistantPlugin(None)
        await plugin.test_connection(base_url="http://192.168.1.50:8123", token="tok")
        assert seen["base_url"] == "http://192.168.1.50:8123"
