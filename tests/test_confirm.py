"""两步确认：签发 → 确认/取消 → 一次性消费。"""

import time

import pytest
from ha_api.services import ServiceCall


class FakeClient:
    """记录调用，不做网络。"""

    def __init__(self):
        self.calls: list[ServiceCall] = []

    async def call(self, call: ServiceCall):
        self.calls.append(call)
        return []

    async def close(self):
        pass


@pytest.fixture
def plugin(plugin_module):
    instance = plugin_module.HomeAssistantPlugin(None)
    instance.client = FakeClient()
    return instance


def unlock_call() -> ServiceCall:
    return ServiceCall(
        domain="lock",
        service="unlock",
        data={"entity_id": "lock.front_door"},
        dangerous=True,
        label="解锁",
    )


def light_call() -> ServiceCall:
    return ServiceCall(
        domain="light",
        service="turn_on",
        data={"entity_id": "light.a"},
        dangerous=False,
        label="打开",
    )


ENTITY = {"entity_id": "lock.front_door", "name": "大门门锁", "caps": {}}


class TestIssue:
    async def test_dangerous_returns_confirmation_required(self, plugin):
        result = await plugin._perform(unlock_call(), ENTITY)
        assert not result.is_ok()
        assert result.error.code == "confirmation_required"
        assert result.error.details["token"]
        assert result.error.details["expires_in"] == 120
        assert "确认" in str(result.error)
        assert plugin.client.calls == []

    async def test_safe_call_executes_directly(self, plugin):
        result = await plugin._perform(light_call(), {"entity_id": "light.a", "name": "客厅灯", "caps": {}})
        assert result.is_ok()
        assert len(plugin.client.calls) == 1

    async def test_new_pending_overwrites_previous(self, plugin):
        await plugin._perform(unlock_call(), ENTITY)
        first_token = next(iter(plugin._pending))
        await plugin._perform(unlock_call(), ENTITY)
        assert len(plugin._pending) == 1
        assert first_token not in plugin._pending


class TestConfirmReply:
    async def test_confirm_executes(self, plugin):
        await plugin._perform(unlock_call(), ENTITY)
        result = await plugin._handle_pending_reply("确认")
        assert result is not None and result.is_ok()
        assert len(plugin.client.calls) == 1
        assert plugin.client.calls[0].service == "unlock"
        assert plugin._pending == {}

    async def test_confirm_is_one_shot(self, plugin):
        await plugin._perform(unlock_call(), ENTITY)
        await plugin._handle_pending_reply("确认")
        second = await plugin._handle_pending_reply("确认")
        assert second is None
        assert len(plugin.client.calls) == 1

    async def test_deny_cancels(self, plugin):
        await plugin._perform(unlock_call(), ENTITY)
        result = await plugin._handle_pending_reply("取消")
        assert result.is_ok()
        assert result.value["cancelled"] is True
        assert plugin.client.calls == []
        assert plugin._pending == {}

    async def test_unrelated_text_not_treated_as_reply(self, plugin):
        await plugin._perform(unlock_call(), ENTITY)
        assert await plugin._handle_pending_reply("打开客厅灯") is None
        assert len(plugin._pending) == 1

    async def test_no_pending_returns_none(self, plugin):
        assert await plugin._handle_pending_reply("确认") is None

    async def test_expired_token_is_pruned(self, plugin):
        await plugin._perform(unlock_call(), ENTITY)
        for pending in plugin._pending.values():
            pending.expires_at = time.time() - 1
        assert await plugin._handle_pending_reply("确认") is None
        assert plugin.client.calls == []
        assert plugin._pending == {}

    async def test_detail_carries_action_info(self, plugin):
        result = await plugin._perform(unlock_call(), ENTITY)
        details = result.error.details
        assert details["service"] == "lock.unlock"
        assert details["entity"] == "大门门锁"
        assert details["danger"] == "解锁"


class TestConfirmWithGarage:
    async def test_garage_confirm_executes_open(self, plugin):
        call = ServiceCall(
            domain="cover",
            service="open_cover",
            data={"entity_id": "cover.garage"},
            dangerous=True,
            label="打开",
        )
        pending_result = await plugin._perform(call, {"entity_id": "cover.garage", "name": "车库门", "caps": {}})
        assert pending_result.error.code == "confirmation_required"
        await plugin._handle_pending_reply("好")
        assert plugin.client.calls[0].service_id == "cover.open_cover"
