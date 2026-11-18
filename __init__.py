"""N.E.K.O 智能家居插件：通过 Home Assistant 控制全平台设备。

设计要点（与 `n.e.k.o_plugin_mijia` 的差异都集中在这几条）：

1. **设备抽象换成实体**。米家是「设备 → siid/aiid 属性」，HA 是「entity_id →
   state + attributes」。所以这里不再有 did/siid，只有 entity_id。
2. **多控开关方位概念消失**。HA 里二键开关天生是两个独立实体，`gang` 字段
   与 `nlp/positions.py` 整个删掉。
3. **危险动作要二次确认**。门锁解锁、车库门开启、警报撤防在 HA 侧比米家更
   常见，所以这里做了插件内自建确认（令牌放进程内存 + 120 秒 TTL + 一次性消费）。
4. **不能连上 HA 也要能启动**。所有控制入口在未配置时返回 `not_configured`，
   不影响宿主加载。

工具面分工沿用米家：控制类走 `@llm_tool`（对话 LLM 调用），查询与配置类走
`@plugin_entry`（analyzer / 面板 / `/runs`）。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from config import USER_PLUGIN_BASE
from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    get_plugin_logger,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    tr,
    ui,
)
from utils.file_utils import atomic_write_json_async, read_json_async

from .ha_api import AsyncHaClient, ConnectionInfo, HaAuthError, HaConnectionError, HaError, HaServiceError
from .ha_api.caps import UNSUPPORTED_DOMAINS
from .ha_api.client import EntitySnapshot, probe_connection
from .ha_api.entities import state_facts
from .ha_api.services import ServiceCall, action_call, power_call, prop_call, scene_call
from .nlp import (
    available_props,
    cap_for_prop,
    current_value,
    find_scene,
    match_devices,
    normalize_utterance,
    prop_bounds,
    resolve_adjust_target,
    resolve_percent_prop,
    route,
    to_command_ir,
    validate_command,
)
from .nlp.intent_terms import DANGER_LABELS, is_confirmation, is_denial
from .nlp.splitter import LEAD_VERB_RE, inherit_verb, split_commands

# 实体缓存 schema 版本。caps 结构变化时必须 +1，否则旧缓存会让校验读不到新字段。
_ENTITIES_CACHE_SCHEMA = 1
# 内存快照有效期。HA 的状态变化对语音控制不敏感，60 秒足够，还能省掉每句话一次全量拉取。
_SNAPSHOT_TTL = 60.0
# 危险动作确认的有效期。太短来不及回复，太长等于没防重放。
_CONFIRM_TTL = 120

_NOT_CONFIGURED_HINT = (
    "还没有连接 Home Assistant。请在插件面板填写 HA 地址（如 http://192.168.1.10:8123）"
    "与长期访问令牌，然后点「保存并连接」。"
)


def _open_url_in_browser(url: str) -> None:
    """在系统默认浏览器中打开 URL（同步，通过 to_thread 调用）。"""
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


def _tool_error(message: str, *, code: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造 @llm_tool 的错误信封。

    `output.code` 放稳定错误码（供程序与模型重试判断），`output.detail` 放模型
    自纠所需的结构化数据（候选实体、允许范围、可选取值），`output.message` 是
    给用户的一句人话。
    """
    return {
        "output": {
            "code": code,
            "detail": detail or {},
            "message": message,
        },
        "is_error": True,
        "error": code,
    }


def _err(code: str, message: str, **detail: Any) -> Any:
    """构造带稳定错误码 + 结构化 detail 的内部 Err。"""
    return Err(SdkError(message, code=code, details=detail or None))


def _tool_envelope(result: Any) -> dict[str, Any]:
    """把内部 Result（Ok/Err）翻成 @llm_tool 的返回信封。

    ⚠️ 内部一切语义失败都必须走 Err——Ok 一律翻成 is_error=false，若某条分支用
    Ok({"success": False}) 表示失败，模型会被告知"成功"。
    """
    if callable(getattr(result, "is_ok", None)):
        if result.is_ok():
            return {"output": result.value, "is_error": False}
        err = result.error
        code = getattr(err, "code", None) or "control_failed"
        details = getattr(err, "details", None)
        return _tool_error(str(err), code=code, detail=details if isinstance(details, dict) else None)
    return {"output": result, "is_error": False}


def _describe_result(command: str, result: Any) -> dict[str, Any]:
    """把单条指令的内部 Result 压成多指令汇总里的一项。"""
    if callable(getattr(result, "is_ok", None)) and result.is_ok():
        value = result.value
        message = value.get("message", "") if isinstance(value, dict) else str(value)
        return {"command": command, "success": True, "message": message}
    err = getattr(result, "error", None)
    return {
        "command": command,
        "success": False,
        "code": getattr(err, "code", None) or "control_failed",
        "message": str(err) if err is not None else "执行失败",
    }


@dataclass
class _PendingConfirm:
    """一次等待确认的危险动作。

    刻意不落盘：进程重启即失效。落盘要额外处理防篡改与过期清理，而收益只是
    "重启后还能继续确认"——用户重说一遍的成本远低于写错的成本。
    """

    token: str
    domain: str
    service: str
    entity_id: str
    entity_name: str
    data: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    prompt: str = ""
    expires_at: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def to_call(self) -> ServiceCall:
        return ServiceCall(domain=self.domain, service=self.service, data=dict(self.data), label=self.label)


SMART_CONTROL_DESCRIPTION = (
    "用一句自然语言控制 Home Assistant 里的智能家居设备，或激活 HA 里预设的场景/脚本。\n\n"
    "何时调用——用户明确要求开关、调节某个设备，或激活某个场景时：\n"
    "  开关：打开客厅灯 / 关掉卧室插座 / 打开窗帘\n"
    "  亮度·色温·颜色：客厅灯亮度调到 50% / 灯调暖一点 / 灯调成蓝色\n"
    "  空调：空调调到 26 度 / 空调制冷 / 风速调到高档\n"
    "  窗帘：窗帘开到一半 / 客厅窗帘关掉\n"
    "  影音：音箱音量 30% / 播放音乐 / 下一首 / 切换音源 HDMI1\n"
    "  场景：执行回家场景 / 晚安\n"
    "不要在闲聊、只是问某个设备现在什么状态、问你能做什么、或想确认上一条指令"
    "到底生效没有时调用——这些都不是控制动作。\n\n"
    "command 参数：\n"
    "  ⚠️ 传用户的原话，不要改写、翻译，也不要自行补充设备名、区域名、数值或单位。\n"
    "  必须是完整的一句话，不要传裸标识符（on / power / 100 这种）。\n"
    "  ✅ 一句话含多个动作时（'打开客厅灯和卧室灯'、'关掉插座，把客厅灯调到50%'），"
    "把整句原话照传即可——本插件会自行拆分、逐条执行并汇总，"
    "不要手动拆成多次调用，也不要漏掉其中任何一个动作。\n\n"
    "返回值：成功时 output 带 success 与 message（实际执行的设备与动作，"
    "message 可直接转述给用户）。\n"
    "多指令时 output 额外带 total 与 results（逐条 command / success / message）。\n"
    "失败时 is_error=true，output 形如 {code, detail, message}：\n"
    "  code 是稳定错误码（如 not_configured / entity_not_found / entity_ambiguous / "
    "attribute_not_found / invalid_unit / invalid_value / enum_value_invalid / "
    "confirmation_required / ha_unreachable / token_expired）。\n"
    "  detail 是可用于重试的结构化数据（候选实体 candidates、可用属性 available、"
    "可选取值 allowed、允许范围 min/max、确认令牌 token）。\n"
    "  message 是给用户的一句人话。\n"
    "⚠️ 收到 confirmation_required 时**不要自己决定执行**：把 message 原样转述给用户，"
    "等用户说'确认'再用同一句确认语调用一次即可；用户说'取消'就放弃。\n"
    "⚠️ 其它失败：能凭 detail 修正就重试一次（例如换用候选设备名、按 min/max 调整数值）；"
    "仍无法确定就如实说明并追问，不要编造执行结果。"
)

SMART_CONTROL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": (
                "用户的自然语言控制指令原话，如'打开客厅灯'、'灯亮度调到50%'、"
                "'空调26度'、'执行回家场景'"
            ),
        },
    },
    "required": ["command"],
}


@neko_plugin
class HomeAssistantPlugin(NekoPluginBase):
    """Home Assistant 智能家居插件。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = get_plugin_logger(__name__)
        self.client: Optional[AsyncHaClient] = None
        self.connection = ConnectionInfo()
        self._lang: str = "zh-CN"
        self._lock = asyncio.Lock()
        self._background_tasks: set = set()
        # 实体快照（内存）。磁盘缓存只在网络失败时兜底。
        self._snapshot: Optional[EntitySnapshot] = None
        self._snapshot_at: float = 0.0
        self._aliases: dict[str, str] = {}
        # 危险动作待确认队列。只保留最新一条，避免攒一堆令牌。
        self._pending: dict[str, _PendingConfirm] = {}

    # ========== 基础工具 ==========
    @staticmethod
    def _resolve_lang() -> str:
        """读取宿主全局语言；独立仓库 / 测试环境下回落到 zh-CN。"""
        try:
            from utils.language_utils import get_global_language_full

            lang = str(get_global_language_full() or "").strip()
            return lang or "zh-CN"
        except Exception:
            return "zh-CN"

    def _t(self, key: str, default: str, **params: Any) -> str:
        """取文案。

        `self.i18n.t(key)` 不传 locale 会退回 plugin.toml 的 default_locale，
        所以必须显式透传宿主语言。独立仓库测试里没有 i18n 对象，用 default
        做 `.format()` 兜底，保证纯逻辑测试不依赖宿主。
        """
        try:
            return self.i18n.t(key, locale=self._lang, default=default, **params)  # type: ignore[attr-defined]
        except Exception:
            try:
                return default.format(**params)
            except (KeyError, IndexError, ValueError):
                return default

    def _data_path(self, name: str = "") -> Path:
        """数据目录下的文件路径；无宿主时退回临时目录（仅测试用）。"""
        try:
            path = self.data_path(name) if name else self.data_path()  # type: ignore[attr-defined]
            return Path(path)
        except Exception:
            base = Path(tempfile.gettempdir()) / "neko_homeassistant"
            base.mkdir(parents=True, exist_ok=True)
            return base / name if name else base

    # ========== 生命周期 ==========
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        """插件启动：解析语言、注册静态 UI、后台加载连接配置。"""
        self.logger.info("Home Assistant 插件启动中...")
        self._lang = self._resolve_lang()

        config_dir = getattr(self, "config_dir", None)
        if config_dir is not None:
            static_dir = Path(config_dir) / "static"
            if static_dir.exists():
                try:
                    ok = self.register_static_ui(  # type: ignore[attr-defined]
                        "static",
                        index_file="index.html",
                        cache_control="no-cache, no-store, must-revalidate",
                    )
                    if ok:
                        self.logger.info("已注册配置页面：/plugin/homeassistant/ui/")
                    else:
                        self.logger.warning("注册静态 UI 失败，请检查 static/index.html")
                except Exception as exc:
                    self.logger.warning(f"注册静态 UI 异常：{exc}")

        task = asyncio.create_task(self._background_load_connection())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return Ok({"status": "ready"})

    async def _background_load_connection(self):
        """后台静默加载连接，不阻塞启动。连不上也不影响插件可用性。"""
        try:
            await self._load_aliases()
            connection = await self._load_connection()
            if connection.configured:
                self.client = AsyncHaClient(connection)
                self.connection = connection
                self.logger.info(f"已加载 Home Assistant 连接：{connection.base_url}")
            else:
                self.logger.info("尚未配置 Home Assistant，请在插件面板填写地址与令牌")
        except Exception as exc:
            self.logger.warning(f"加载连接配置失败：{exc}")

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        """插件关闭：取消后台任务、关闭 HTTP 连接。"""
        self.logger.info("Home Assistant 插件关闭")
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as exc:
                self.logger.warning(f"关闭 HA 客户端出错：{exc}")
            finally:
                self.client = None
        self._pending.clear()
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        """配置变化时重新加载连接。"""
        await self._reload_connection()
        return Ok({"reloaded": self.client is not None})

    # ========== 连接与凭据 ==========
    async def _load_connection(self) -> ConnectionInfo:
        path = self._data_path("credential.json")
        try:
            raw = await read_json_async(path)
        except Exception:
            return ConnectionInfo()
        return ConnectionInfo.from_dict(raw)

    async def _save_connection(self, connection: ConnectionInfo) -> None:
        path = self._data_path("credential.json")
        text = json.dumps(connection.to_dict(), ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._atomic_write_secret, path, text)

    @staticmethod
    def _atomic_write_secret(path: Path, text: str) -> None:
        """原子写敏感文件：临时文件 → 收紧权限 → 写内容 → replace。

        直接 `write_text` 有两个问题：写入中途崩溃会留下半截文件；文件在
        默认权限下先落地，存在一个短暂的"任何用户可读"窗口。令牌就是凭据，
        这个窗口不能留。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_cred_", suffix=".json")
        try:
            if sys.platform != "win32":
                os.chmod(tmp_name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _harden_permissions(path: Path) -> None:
        """把文件权限收紧到仅当前用户（Windows 用 icacls，其它用 chmod）。

        ⚠️ icacls 的输出是本地代码页（GBK），**不能**传 `text=True`，否则在
        非 UTF-8 环境下会解码报错。
        """
        try:
            if sys.platform == "win32":
                username = os.environ.get("USERNAME", "").strip()
                if not username:
                    return
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
                    capture_output=True,
                    check=False,
                )
            else:
                os.chmod(path, 0o600)
        except Exception:
            # 权限收紧失败不应阻断保存——文件已经写好了，只是没那么严。
            pass

    async def _reload_connection(self) -> None:
        async with self._lock:
            connection = await self._load_connection()
            old_client = self.client
            self.connection = connection
            self.client = AsyncHaClient(connection) if connection.configured else None
            self._invalidate_snapshot()
            if old_client is not None:
                try:
                    await old_client.close()
                except Exception as exc:
                    self.logger.warning(f"关闭旧 HA 客户端出错：{exc}")

    # ========== 别名 ==========
    async def _load_aliases(self) -> None:
        path = self._data_path("aliases.json")
        try:
            raw = await read_json_async(path)
        except Exception:
            self._aliases = {}
            return
        if isinstance(raw, dict) and isinstance(raw.get("aliases"), dict):
            self._aliases = {str(k): str(v) for k, v in raw["aliases"].items() if str(v).strip()}
        else:
            self._aliases = {}

    async def _save_aliases(self) -> None:
        path = self._data_path("aliases.json")
        await atomic_write_json_async(path, {"aliases": self._aliases})

    # ========== 实体快照 ==========
    def _invalidate_snapshot(self) -> None:
        self._snapshot = None
        self._snapshot_at = 0.0

    async def _get_snapshot(self, *, force: bool = False) -> EntitySnapshot:
        """取实体快照：内存 → 网络 → 磁盘。

        磁盘兜底的意义：HA 通常在内网，网络抖动或 HA 正在重启时，用几分钟前的
        实体表仍能把指令发出去，比直接报"设备列表获取失败"有用得多。
        """
        if self.client is None:
            raise HaError("尚未配置 Home Assistant")

        now = time.monotonic()
        if not force and self._snapshot is not None and (now - self._snapshot_at) < _SNAPSHOT_TTL:
            return self._snapshot

        try:
            snapshot = await self.client.collect_entities(aliases=self._aliases)
        except Exception as exc:
            self.logger.warning(f"刷新实体失败，尝试本地缓存：{exc}")
            cached = await self._read_snapshot_cache()
            if cached is not None:
                cached.degraded = f"{cached.degraded or '使用本地缓存'}（刷新失败：{exc}）"
                self._snapshot = cached
                self._snapshot_at = now
                return cached
            raise

        self._snapshot = snapshot
        self._snapshot_at = now
        await self._write_snapshot_cache(snapshot)
        return snapshot

    async def _read_snapshot_cache(self) -> Optional[EntitySnapshot]:
        path = self._data_path("entities_cache.json")
        try:
            raw = await read_json_async(path)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("schema_version") != _ENTITIES_CACHE_SCHEMA:
            return None
        # 换了 HA 实例就不能用旧缓存，否则会把上一台 HA 的实体当成本机的。
        if raw.get("base_url") != self.connection.base_url:
            return None
        entities = raw.get("entities")
        if not isinstance(entities, list):
            return None
        return EntitySnapshot(
            entities=[item for item in entities if isinstance(item, dict)],
            area_count=int(raw.get("area_count") or 0),
            ws_used=bool(raw.get("ws_used")),
            degraded=str(raw.get("degraded") or ""),
            ha_version=str(raw.get("ha_version") or ""),
            location_name=str(raw.get("location_name") or ""),
        )

    async def _write_snapshot_cache(self, snapshot: EntitySnapshot) -> None:
        path = self._data_path("entities_cache.json")
        payload = {
            "schema_version": _ENTITIES_CACHE_SCHEMA,
            "base_url": self.connection.base_url,
            "fetched_at": time.time(),
            "area_count": snapshot.area_count,
            "ws_used": snapshot.ws_used,
            "degraded": snapshot.degraded,
            "ha_version": snapshot.ha_version,
            "location_name": snapshot.location_name,
            "entities": snapshot.entities,
        }
        try:
            await atomic_write_json_async(path, payload)
        except Exception as exc:
            # 缓存写失败不影响控制，只是下次要重新拉。
            self.logger.debug(f"写入实体缓存失败：{exc}")

    # ========== UI 上下文 ==========
    @ui.context(id="dashboard", title="Home Assistant 智能家居")
    async def get_dashboard_context(self):
        """为 Hosted UI 面板提供状态数据。"""
        configured = self.connection.configured
        entities: list[dict] = []
        degraded = ""
        area_count = 0

        if self.client is not None:
            try:
                snapshot = await self._get_snapshot()
                entities = snapshot.entities
                degraded = snapshot.degraded
                area_count = snapshot.area_count
            except Exception as exc:
                degraded = str(exc)

        domain_counts: dict[str, int] = {}
        for entity in entities:
            domain = entity.get("domain", "")
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        return {
            "configured": configured,
            "base_url": self.connection.base_url,
            "location_name": self.connection.location_name,
            "ha_version": self.connection.ha_version,
            "degraded": degraded,
            "area_count": area_count,
            "entity_count": len(entities),
            "domain_counts": domain_counts,
            "entities": entities,
        }

    @plugin_entry(
        id="open_ui",
        name=tr("entries.open_ui.name", default="打开配置页面"),
        description=tr("entries.open_ui.description", default="在浏览器中打开本插件的配置面板"),
        kind="action",
    )
    async def open_ui(self, **_):
        """在浏览器中打开配置面板。"""
        url = f"{USER_PLUGIN_BASE}/plugin/homeassistant/ui/"
        try:
            await asyncio.to_thread(_open_url_in_browser, url)
            return Ok({"success": True, "url": url, "message": "已在浏览器打开配置页面"})
        except Exception as exc:
            self.logger.exception("打开配置页面失败")
            return Err(SdkError(f"打开配置页面失败：{exc}"))

    @plugin_entry(
        id="test_connection",
        name=tr("entries.test_connection.name", default="测试连接"),
        description=tr("entries.test_connection.description", default="校验 Home Assistant 地址与长期访问令牌是否可用"),
        input_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "description": "如 http://192.168.1.10:8123"},
                "token": {"type": "string", "description": "长期访问令牌"},
            },
            "required": ["base_url", "token"],
        },
    )
    async def test_connection(self, base_url: str = "", token: str = "", **_):
        """只探测不保存。面板的"测试连接"按钮调这个。"""
        if not base_url or not token:
            return _err("missing_parameter", "请填写 Home Assistant 地址与令牌")
        try:
            info = await probe_connection(base_url, token)
        except HaAuthError as exc:
            return _err("token_expired", str(exc))
        except HaConnectionError as exc:
            return _err("ha_unreachable", str(exc))
        except Exception as exc:
            return _err("connection_failed", f"连接失败：{exc}")
        return Ok(
            {
                "success": True,
                "message": f"连接成功：{info.location_name or 'Home Assistant'}（{info.ha_version or '未知版本'}）",
                "base_url": info.base_url,
                "ha_version": info.ha_version,
                "location_name": info.location_name,
            }
        )

    @plugin_entry(
        id="save_connection",
        name=tr("entries.save_connection.name", default="保存连接"),
        description=tr("entries.save_connection.description", default="验证并保存 Home Assistant 地址与令牌"),
        input_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "description": "如 http://192.168.1.10:8123"},
                "token": {"type": "string", "description": "长期访问令牌"},
            },
            "required": ["base_url", "token"],
        },
    )
    async def save_connection(self, base_url: str = "", token: str = "", **_):
        """保存前**必须**探活：存下连不上的地址，之后每次控制都要等一次超时。"""
        if not base_url or not token:
            return _err("missing_parameter", "请填写 Home Assistant 地址与令牌")
        try:
            info = await probe_connection(base_url, token)
        except HaAuthError as exc:
            return _err("token_expired", str(exc))
        except HaConnectionError as exc:
            return _err("ha_unreachable", str(exc))
        except Exception as exc:
            return _err("connection_failed", f"连接失败：{exc}")

        await self._save_connection(info)
        await asyncio.to_thread(self._harden_permissions, self._data_path("credential.json"))
        self.connection = info
        if self.client is not None:
            await self.client.close()
        self.client = AsyncHaClient(info)
        self._invalidate_snapshot()
        self.logger.info(f"已保存 Home Assistant 连接：{info.base_url}")
        return Ok(
            {
                "success": True,
                "message": f"已连接：{info.location_name or 'Home Assistant'}",
                "base_url": info.base_url,
                "ha_version": info.ha_version,
            }
        )

    @plugin_entry(
        id="get_status",
        name=tr("entries.get_status.name", default="查看连接状态"),
        description=tr("entries.get_status.description", default="返回当前连接信息与设备统计，供面板首屏渲染"),
    )
    async def get_status(self, **_):
        """面板首屏用：连接信息（令牌脱敏）+ 缓存统计。

        刻意不触发网络请求——面板打开就卡住等 HA 响应是最糟的体验。
        需要最新数据时面板会另外调 reload_entities。
        """
        snapshot = self._snapshot or await self._read_snapshot_cache()
        entities = snapshot.entities if snapshot else []
        return Ok(
            {
                "success": True,
                "connection": self.connection.public(),
                "entity_count": len(entities),
                "area_count": snapshot.area_count if snapshot else 0,
                "degraded": snapshot.degraded if snapshot else "",
                "entities": entities,
            }
        )

    @plugin_entry(
        id="clear_connection",
        name=tr("entries.clear_connection.name", default="断开连接"),
        description=tr("entries.clear_connection.description", default="删除保存的地址与令牌，并清空实体缓存"),
        kind="action",
    )
    async def clear_connection(self, **_):
        """断开连接并清空本地数据。"""
        for name in ("credential.json", "entities_cache.json", "aliases.json"):
            path = self._data_path(name)
            try:
                if path.exists():
                    await asyncio.to_thread(path.unlink)
            except Exception as exc:
                self.logger.warning(f"删除 {name} 失败：{exc}")

        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
        self.client = None
        self.connection = ConnectionInfo()
        self._aliases = {}
        self._pending.clear()
        self._invalidate_snapshot()
        return Ok({"success": True, "message": "已断开 Home Assistant 连接"})

    @plugin_entry(
        id="reload_entities",
        name=tr("entries.reload_entities.name", default="刷新设备列表"),
        description=tr("entries.reload_entities.description", default="从 Home Assistant 重新拉取实体、区域与状态"),
        llm_result_fields=["message"],
    )
    async def reload_entities(self, **_):
        """强制刷新实体快照。"""
        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)
        try:
            snapshot = await self._get_snapshot(force=True)
        except HaAuthError as exc:
            return _err("token_expired", str(exc))
        except HaConnectionError as exc:
            return _err("ha_unreachable", str(exc))
        except Exception as exc:
            return _err("entity_list_unavailable", f"获取实体列表失败：{exc}")
        return Ok(
            {
                "success": True,
                "message": f"已刷新 {len(snapshot.entities)} 个实体（{snapshot.area_count} 个区域）",
                "count": len(snapshot.entities),
                "area_count": snapshot.area_count,
                "degraded": snapshot.degraded,
            }
        )

    @plugin_entry(
        id="list_entities",
        name=tr("entries.list_entities.name", default="列出设备"),
        description=tr("entries.list_entities.description", default="列出可从自然语言控制的实体，可按 domain 或区域过滤"),
        input_schema={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "如 light / climate / cover，留空为全部"},
                "area": {"type": "string", "description": "区域名，留空为全部"},
            },
            "required": [],
        },
        llm_result_fields=["message"],
    )
    async def list_entities(self, domain: str = "", area: str = "", **_):
        """列出可控实体。"""
        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)
        try:
            snapshot = await self._get_snapshot()
        except Exception as exc:
            return _err("entity_list_unavailable", f"获取实体列表失败：{exc}")

        items = snapshot.entities
        if domain:
            items = [item for item in items if item.get("domain") == domain]
        if area:
            items = [item for item in items if item.get("area_name") == area]

        lines = [f"共 {len(items)} 个设备："]
        for item in items[:60]:
            state = item.get("state", "")
            label = f"{item.get('area_name', '')} {item.get('name', '')}".strip()
            alias = item.get("alias", "")
            suffix = f"（别名：{alias}）" if alias else ""
            lines.append(f"  • {label}{suffix} — {state}")
        if len(items) > 60:
            lines.append(f"  …另有 {len(items) - 60} 个")

        return Ok(
            {
                "success": True,
                "message": "\n".join(lines),
                "entities": items,
                "count": len(items),
            }
        )

    @plugin_entry(
        id="list_areas",
        name=tr("entries.list_areas.name", default="列出区域"),
        description=tr("entries.list_areas.description", default="列出 Home Assistant 里的区域（房间）及其设备数量"),
        llm_result_fields=["message"],
    )
    async def list_areas(self, **_):
        """按区域汇总实体。"""
        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)
        try:
            snapshot = await self._get_snapshot()
        except Exception as exc:
            return _err("entity_list_unavailable", f"获取实体列表失败：{exc}")

        buckets: dict[str, int] = {}
        for item in snapshot.entities:
            name = item.get("area_name") or "未分配区域"
            buckets[name] = buckets.get(name, 0) + 1

        lines = [f"共 {len(buckets)} 个区域："]
        for name, count in sorted(buckets.items()):
            lines.append(f"  • {name}（{count} 个设备）")
        if snapshot.degraded:
            lines.append(f"⚠️ {snapshot.degraded}")

        return Ok(
            {
                "success": True,
                "message": "\n".join(lines),
                "areas": buckets,
                "degraded": snapshot.degraded,
            }
        )

    @plugin_entry(
        id="set_entity_alias",
        name=tr("entries.set_entity_alias.name", default="设置别名"),
        description=tr("entries.set_entity_alias.description", default="给实体设一个好叫的中文别名，留空则删除"),
        input_schema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "如 light.living_room"},
                "alias": {"type": "string", "description": "别名，留空删除"},
            },
            "required": ["entity_id"],
        },
        llm_result_fields=["message"],
    )
    async def set_entity_alias(self, entity_id: str = "", alias: str = "", **_):
        """设置实体别名。别名只影响本插件的匹配，不改 HA。"""
        if not entity_id:
            return _err("missing_parameter", "请提供 entity_id")
        alias = (alias or "").strip()
        if alias:
            self._aliases[entity_id] = alias
        else:
            self._aliases.pop(entity_id, None)
        await self._save_aliases()
        self._invalidate_snapshot()
        message = f"已为 {entity_id} 设置别名「{alias}」" if alias else f"已删除 {entity_id} 的别名"
        return Ok({"success": True, "message": message, "entity_id": entity_id, "alias": alias})

    @plugin_entry(
        id="get_entity_aliases",
        name=tr("entries.get_entity_aliases.name", default="查看别名"),
        description=tr("entries.get_entity_aliases.description", default="列出所有已设置的实体别名"),
        llm_result_fields=["message"],
    )
    async def get_entity_aliases(self, **_):
        if not self._aliases:
            return Ok({"success": True, "message": "还没有设置任何别名", "aliases": {}})
        lines = [f"共 {len(self._aliases)} 个别名："]
        for entity_id, alias in self._aliases.items():
            lines.append(f"  • {alias} → {entity_id}")
        return Ok({"success": True, "message": "\n".join(lines), "aliases": dict(self._aliases)})

    @plugin_entry(
        id="query_entity_state",
        name=tr("entries.query_entity_state.name", default="查询设备状态"),
        description=tr("entries.query_entity_state.description", default="查询某个设备当前的状态，如温度、亮度、开关"),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "设备名或别名，如 客厅灯"}},
            "required": ["name"],
        },
        llm_result_fields=["message"],
    )
    async def query_entity_state(self, name: str = "", **_):
        """查询状态。按名字匹配实体后读实时状态。"""
        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)
        if not name:
            return _err("missing_parameter", "请提供要查询的设备名")
        try:
            snapshot = await self._get_snapshot()
        except Exception as exc:
            return _err("entity_list_unavailable", f"获取设备列表失败：{exc}")
        return await self._query_by_name(name, snapshot.entities)

    @plugin_entry(
        id="get_cached_entities",
        name=tr("entries.get_cached_entities.name", default="读取缓存的设备列表"),
        description=tr("entries.get_cached_entities.description", default="直接读取本地缓存的实体列表，不访问 Home Assistant"),
        llm_result_fields=["message"],
    )
    async def get_cached_entities(self, **_):
        """只读缓存，用于面板首屏快速渲染。"""
        snapshot = await self._read_snapshot_cache()
        if snapshot is None:
            return Ok({"success": True, "message": "暂无缓存", "entities": [], "count": 0})
        return Ok(
            {
                "success": True,
                "message": f"缓存中有 {len(snapshot.entities)} 个实体",
                "entities": snapshot.entities,
                "count": len(snapshot.entities),
                "degraded": snapshot.degraded,
            }
        )

    # ========== 对话工具 ==========
    @llm_tool(
        name="smart_control",
        description=SMART_CONTROL_DESCRIPTION,
        parameters=SMART_CONTROL_SCHEMA,
        # 单次控制最长等待：HA 的服务调用会同步等待动作完成，局域网内通常
        # 几百毫秒；15s 足够覆盖一次冷启动的 WS 注册表拉取。
        timeout=30.0,
    )
    async def smart_control(self, *, command: Any = None, **_) -> dict[str, Any]:
        """对话 LLM 直接调用的设备控制工具。

        只挂 @llm_tool、不再挂 @plugin_entry：宿主会把 analyzer 看的入口列表里的
        __llm_tool__* 剥掉，两边不会对同一条指令各执行一次。
        """
        # command 声明成 Any = None 而非必填 str：LLM 违反 schema 时也要能返回
        # 一句读得懂的错误，而不是在分发处抛 TypeError。
        if not isinstance(command, str) or not command.strip():
            return _tool_error(
                "缺少 command 参数：请把用户的原话作为 command 传入，如 '打开客厅灯'。",
                code="missing_command",
                detail={"expected": "non-empty string", "param": "command"},
            )
        return _tool_envelope(await self._dispatch_smart_control(command.strip()))

    async def _dispatch_smart_control(self, command: str) -> Any:
        """统一分发核心，返回内部 Result（Ok/Err）。"""
        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)

        # 原始指令属会话文本，按仓库规范用 print 而非 logger
        print(f"[smart_control] 指令: {command}", flush=True)

        # 先处理待确认动作的答复（"确认"/"取消"），它必须先于解析——
        # 否则"确认"会被当成一句无法理解的指令。
        reply = await self._handle_pending_reply(command)
        if reply is not None:
            return reply

        segments = split_commands(command)
        if len(segments) > 1:
            multi = await self._dispatch_multi(segments)
            if multi is not None:
                return multi
        return await self._dispatch_single(command)

    async def _dispatch_single(self, command: str, *, entities: Optional[list] = None) -> Any:
        """执行单条指令（多指令拆分后的每一段也走这里）。"""
        norm = normalize_utterance(command)
        try:
            snapshot = await self._get_snapshot()
        except HaAuthError as exc:
            return _err("token_expired", str(exc))
        except HaConnectionError as exc:
            return _err("ha_unreachable", str(exc))
        except Exception as exc:
            return _err("entity_list_unavailable", f"无法获取设备列表：{exc}")

        # 控制候选与全量实体分开：动作/开关/属性只能落在可控实体上，
        # 但查询要能命中只读传感器（"客厅温度多少"）。
        control_pool = entities if entities is not None else snapshot.entities
        result = await route(norm, control_pool)
        self.logger.info(f"路由结果: branch={result.branch}")
        print(
            f"[smart_control] 路由: branch={result.branch}, "
            f"device={result.device_hint!r}, parsed={result.parsed!r}",
            flush=True,
        )

        if result.branch == "scene":
            return await self._execute_scene(result, snapshot.entities)
        if result.branch == "query":
            return await self._query_by_name(result.device_hint, snapshot.entities, prop=result.query_prop)

        ir = to_command_ir(result, raw_text=command)
        if result.branch == "action":
            return await self._execute_action(ir)
        if result.branch in ("switch", "control"):
            return await self._execute_control(ir)

        return Err(
            SdkError(
                "无法理解命令。支持的格式：\n"
                "  开关：'打开客厅灯' / '关掉插座'\n"
                "  亮度：'灯调到50%' / '灯调暗一点'\n"
                "  温度：'空调26度' / '空调制冷'\n"
                "  窗帘：'窗帘开到一半'\n"
                "  音量：'音箱音量30%'\n"
                "  场景：'执行回家场景'"
            )
        )

    # ── 多指令 ──────────────────────────────────────────────────────────
    async def _dispatch_multi(self, segments: list[str]) -> Any:
        """逐条执行拆分后的指令并汇总；拆分不成立时返回 None 让调用方回退整句。

        采用拆分的前提是**每一段都能路由到明确目标**。任一段路由不了就整体
        回退——否则"和风空调"这类设备名被连词切坏后，前半段会当成一条指令
        去执行，产生原句根本没有的动作。
        """
        try:
            snapshot = await self._get_snapshot()
        except Exception:
            return None

        resolved: list[str] = []
        shared_verb = ""
        for segment in segments:
            text = await self._resolve_segment(segment, shared_verb, snapshot.entities)
            if text is None:
                return None
            resolved.append(text)
            verb_match = LEAD_VERB_RE.match(text)
            if verb_match:
                shared_verb = verb_match.group(1)

        print(f"[smart_control] 多指令: {resolved}", flush=True)
        return await self._execute_multi(resolved, snapshot.entities)

    async def _resolve_segment(self, segment: str, shared_verb: str, devices: list) -> Optional[str]:
        """判断单独一段能否执行，返回最终要执行的文本（可能补了共享动词）。"""
        if await self._segment_routable(segment, devices):
            return segment
        candidate = inherit_verb(segment, shared_verb)
        if candidate != segment and await self._segment_routable(candidate, devices):
            return candidate
        return None

    @staticmethod
    async def _segment_routable(segment: str, devices: list) -> bool:
        """该段能否路由到明确目标。

        场景与查询放行；开关/动作/控制要求实体匹配成功——匹配不上说明这段不是
        有效指令，整句回退比强行执行安全。
        """
        result = await route(normalize_utterance(segment), devices)
        if result.branch in ("scene", "query"):
            return True
        if result.branch == "unknown":
            return False
        match = result.match
        return match is not None and match.status == "ok"

    async def _execute_multi(self, segments: list[str], devices: list) -> Any:
        """串行执行各段并汇总。

        串行而非并发：同一条指令里的多个动作往往有先后意图（先关灯再开夜灯），
        并发会打乱顺序；多指令场景也不追求最低延迟。
        """
        results: list[dict] = []
        for segment in segments:
            item = _describe_result(segment, await self._dispatch_single(segment, entities=devices))
            results.append(item)
        succeeded = sum(1 for item in results if item["success"])
        failed = len(results) - succeeded

        lines = [f"{len(results)} 条指令：{succeeded} 成功" + (f"，{failed} 失败" if failed else "")]
        for item in results:
            if item["success"]:
                lines.append(f"  ✅ {item['command']}")
            else:
                lines.append(f"  ❌ {item['command']}：{item['message']}")
        message = "\n".join(lines)

        if not failed:
            return Ok({"success": True, "total": len(results), "results": results, "message": message})
        # 部分或全部失败都走 Err：Ok 会被翻成 is_error=false，模型会以为全部生效。
        return _err(
            "multi_control_failed" if succeeded == 0 else "multi_control_partial_failure",
            message,
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    # ── 执行分支 ────────────────────────────────────────────────────────
    async def _execute_scene(self, result: Any, entities: list) -> Any:
        """场景/脚本激活。"""
        keywords = list(getattr(result, "scene_keywords", None) or [])
        entity = find_scene(entities, keywords)
        if entity is None:
            available = [item.get("name", "") for item in entities if item.get("domain") in ("scene", "script")]
            return _err(
                "scene_not_found",
                f"没找到场景「{keywords[0] if keywords else ''}」。现有场景：{', '.join(available[:10]) or '无'}",
                keywords=keywords,
                available=available[:20],
            )
        return await self._perform(scene_call(entity), entity)

    async def _execute_control(self, ir: Any) -> Any:
        """开关 / 属性 / 相对调整三条路径的统一入口。"""
        if ir.match_status == "ambiguous":
            return _err("entity_ambiguous", ir.match_message or "找到多个匹配设备", candidates=ir.candidates)
        if ir.match_status != "ok" or ir.entity_info is None:
            return _err(
                "entity_not_found",
                ir.match_message or f"未找到设备「{ir.device}」",
                device=ir.device,
                candidates=ir.candidates,
            )

        entity = ir.entity_info
        domain = entity.get("domain", "")
        if domain in UNSUPPORTED_DOMAINS:
            return _err(
                "unsupported_domain",
                f"暂不支持控制{UNSUPPORTED_DOMAINS[domain]}（{domain}），首版支持灯光、开关、空调、窗帘、影音与场景",
                domain=domain,
            )

        if ir.action == "switch":
            call = power_call(entity, bool(ir.value))
            if call is None:
                return _err(
                    "action_not_supported",
                    f"「{entity.get('name')}」不支持开关操作",
                    device=entity.get("name"),
                    domain=domain,
                )
            return await self._perform(call, entity)

        prop = resolve_percent_prop(entity, ir.attribute)
        if ir.action == "adjust_prop":
            return await self._execute_adjust(entity, prop, ir)
        return await self._execute_set(entity, prop, ir)

    async def _execute_set(self, entity: dict, prop: str, ir: Any) -> Any:
        """绝对值设定（含极值 max/min 与颜色）。"""
        cap = cap_for_prop(entity, prop)
        value = ir.value

        if isinstance(value, str) and value in ("max", "min"):
            bounds = prop_bounds(entity, cap)
            if not bounds:
                return _err(
                    "attribute_not_supported",
                    f"「{entity.get('name')}」不支持调节「{prop}」",
                    attribute=prop,
                    available=available_props(entity),
                )
            value = bounds[1] if value == "max" else bounds[0]

        validation = validate_command(entity, prop, value, ir.unit)
        if validation.status != "valid":
            return _err(validation.status, validation.message, **validation.detail)

        call = prop_call(entity, cap, validation.resolved)
        if call is None:
            return _err(
                "attribute_not_supported",
                f"「{entity.get('name')}」不支持「{prop}」",
                attribute=prop,
                available=available_props(entity),
            )
        return await self._perform(call, entity)

    async def _execute_adjust(self, entity: dict, prop: str, ir: Any) -> Any:
        """相对调整（"调暗一点" / "温度调高两度"）。

        必须先读到当前值才能算目标值。读不到就报错让用户给绝对值——用区间中点
        瞎猜会导致"调暗一点"把灯调到 50%，比报错更糟。
        """
        cap = cap_for_prop(entity, prop)
        if not cap:
            return _err(
                "attribute_not_supported",
                f"「{entity.get('name')}」不支持调节「{prop}」",
                attribute=prop,
                available=available_props(entity),
            )

        current = current_value(entity, cap)
        if current is None:
            return _err(
                "current_value_unknown",
                f"读取不到「{entity.get('name')}」的当前{prop}，请直接给出目标值（如「{prop}50%」）",
                attribute=prop,
                suggest="absolute_value",
            )

        bounds = prop_bounds(entity, cap) or [0, 100, 1]
        target = resolve_adjust_target(current, ir.direction, ir.delta, bounds)

        validation = validate_command(entity, prop, target, "")
        if validation.status != "valid":
            return _err(validation.status, validation.message, **validation.detail)

        call = prop_call(entity, cap, validation.resolved)
        if call is None:
            return _err(
                "attribute_not_supported",
                f"「{entity.get('name')}」不支持调节「{prop}」",
                attribute=prop,
                available=available_props(entity),
            )
        self.logger.info(f"相对调整: {prop} {current} → {target}")
        return await self._perform(call, entity)

    async def _execute_action(self, ir: Any) -> Any:
        """媒体/窗帘/扫地机的动作型指令。"""
        if ir.match_status == "ambiguous":
            return _err("entity_ambiguous", ir.match_message or "找到多个匹配设备", candidates=ir.candidates)
        if ir.match_status != "ok" or ir.entity_info is None:
            return _err(
                "entity_not_found",
                ir.match_message or f"未找到设备「{ir.device}」",
                device=ir.device,
                candidates=ir.candidates,
            )

        entity = ir.entity_info
        call = action_call(entity, ir.verb)
        if call is None:
            caps = entity.get("caps") or {}
            return _err(
                "action_not_supported",
                f"「{entity.get('name')}」不支持「{ir.verb}」",
                device=entity.get("name"),
                verb=ir.verb,
                available=caps.get("media_actions") or [],
            )
        return await self._perform(call, entity)

    # ── 查询 ────────────────────────────────────────────────────────────
    async def _query_by_name(self, name: str, entities: list, *, prop: str = "") -> Any:
        """按名字匹配实体并返回当前状态。

        ``prop`` 是从原话里剥出来的属性后缀（"客厅温度多少度" → name="客厅",
        prop="温度"）。**必须拼回去再试一次**，否则"客厅"会同时命中吸顶灯和
        温度传感器，直接变成歧义错误。
        """
        if not name:
            return _err("missing_parameter", "要查询哪个设备？")

        candidates = [f"{name}{prop}", name] if prop else [name]
        match = None
        for candidate in candidates:
            current = match_devices(candidate, entities)
            if current.status == "ok":
                match = current
                break
            if match is None:
                match = current
        assert match is not None

        if match.status == "ambiguous":
            return _err("entity_ambiguous", match.message, candidates=match.candidates)
        if match.status != "ok" or match.device is None:
            return _err("entity_not_found", match.message, device=name, candidates=match.candidates)

        entity = dict(match.device)
        # 缓存里的 state 可能已经过期几十秒，查询时尽量读实时状态。
        if self.client is not None:
            try:
                fresh = await self.client.get_entity_state(entity.get("entity_id", ""))
                if fresh:
                    entity["state"] = str(fresh.get("state") or entity.get("state"))
                    attributes = fresh.get("attributes") or {}
                    if isinstance(attributes, dict):
                        entity["attributes"] = {**entity.get("attributes", {}), **attributes}
            except Exception as exc:
                self.logger.debug(f"读取实时状态失败，使用缓存：{exc}")

        facts = state_facts(entity)
        message = self._describe_state(entity, facts)
        return Ok(
            {
                "success": True,
                "message": message,
                "entity_id": entity.get("entity_id"),
                "name": entity.get("name"),
                "state": entity.get("state"),
                "facts": facts,
            }
        )

    def _describe_state(self, entity: dict, facts: dict) -> str:
        """把语义事实拼成人话。文案走 i18n，事实来自 `state_facts()`。"""
        name = entity.get("name") or entity.get("entity_id") or ""
        domain = entity.get("domain", "")

        if not entity.get("is_online", True):
            return self._t("state.unavailable", "「{name}」当前离线", name=name)

        if domain in ("light", "switch", "input_boolean"):
            if not facts.get("power"):
                return self._t("state.off", "「{name}」已关闭", name=name)
            if "brightness" in facts:
                return self._t(
                    "state.on_with_brightness", "「{name}」已开启，亮度 {brightness}%", name=name, **facts
                )
            return self._t("state.on", "「{name}」已开启", name=name)

        if domain == "climate":
            parts = [self._t("state.hvac_mode", "模式 {mode}", mode=facts.get("hvac_mode", ""))]
            if "current_temperature" in facts:
                parts.append(
                    self._t("state.current_temp", "当前 {temp}°C", temp=facts["current_temperature"])
                )
            if "target_temperature" in facts:
                parts.append(self._t("state.target_temp", "目标 {temp}°C", temp=facts["target_temperature"]))
            if "fan_mode" in facts:
                parts.append(self._t("state.fan_mode", "风速 {mode}", mode=facts["fan_mode"]))
            return self._t("state.climate", "「{name}」{detail}", name=name, detail="，".join(parts))

        if domain == "cover":
            if facts.get("position") is not None:
                return self._t(
                    "state.cover_position", "「{name}」当前开合度 {position}%", name=name, **facts
                )
            state_text = self._t("state.cover_open", "开启", name=name) if facts.get("open") else self._t(
                "state.cover_closed", "关闭", name=name
            )
            return self._t("state.cover", "「{name}」{state}", name=name, state=state_text)

        if domain == "media_player":
            parts: list[str] = []
            if "volume" in facts:
                parts.append(self._t("state.volume", "音量 {volume}%", volume=facts["volume"]))
            if "source" in facts:
                parts.append(self._t("state.source", "音源 {source}", source=facts["source"]))
            if "media_title" in facts:
                parts.append(self._t("state.media_title", "正在播放 {title}", title=facts["media_title"]))
            detail = "，".join(parts) or facts.get("state", "")
            return self._t("state.media", "「{name}」{detail}", name=name, detail=detail)

        if domain == "lock":
            key = "state.locked" if facts.get("locked") else "state.unlocked"
            default = "「{name}」已上锁" if facts.get("locked") else "「{name}」未上锁"
            return self._t(key, default, name=name)

        if domain in ("sensor", "binary_sensor"):
            unit = facts.get("unit", "")
            return self._t(
                "state.sensor", "「{name}」当前为 {value}{unit}", name=name, value=facts.get("value", ""), unit=unit
            )

        return self._t("state.generic", "「{name}」当前状态：{state}", name=name, state=facts.get("state", ""))

    # ── 执行与确认 ──────────────────────────────────────────────────────
    async def _perform(self, call: ServiceCall, entity: dict, *, allow_dangerous: bool = False) -> Any:
        """执行服务调用；危险动作先走确认流程。"""
        if call.dangerous and not allow_dangerous:
            return self._issue_confirmation(call, entity)

        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)

        name = entity.get("name") or entity.get("entity_id") or ""
        try:
            await self.client.call(call)
        except HaAuthError as exc:
            return _err("token_expired", str(exc))
        except HaConnectionError as exc:
            return _err("ha_unreachable", str(exc))
        except HaServiceError as exc:
            return _err("service_failed", str(exc), service=call.service_id, detail=getattr(exc, "detail", None))
        except Exception as exc:
            return _err("service_failed", f"调用 {call.service_id} 失败：{exc}", service=call.service_id)

        return Ok(
            {
                "success": True,
                "message": self._t("messages.done", "✅ 已{label}「{name}」", label=call.label, name=name),
                "entity": name,
                "entity_id": entity.get("entity_id", ""),
                "service": call.service_id,
                "data": call.data,
            }
        )

    def _issue_confirmation(self, call: ServiceCall, entity: dict) -> Any:
        """签发一个待确认令牌，返回 confirmation_required 错误。

        令牌是 8 位十六进制随机串，只存在进程内存里，120 秒后失效且一次性使用。
        同时只保留最近一条：连续说"解锁大门"两次不应该攒出两个待执行动作。
        """
        self._prune_pending()
        token = secrets.token_hex(4)
        name = entity.get("name") or call.data.get("entity_id") or ""
        action = DANGER_LABELS.get(call.service, call.label or call.service)
        prompt = self._t(
            "messages.confirm",
            "即将对「{name}」执行「{action}」，确认吗？回复「确认」继续，回复「取消」放弃（{seconds} 秒内有效）",
            name=name,
            action=action,
            seconds=_CONFIRM_TTL,
        )
        pending = _PendingConfirm(
            token=token,
            domain=call.domain,
            service=call.service,
            entity_id=entity.get("entity_id", ""),
            entity_name=name,
            data=dict(call.data),
            label=call.label,
            prompt=prompt,
            expires_at=time.time() + _CONFIRM_TTL,
        )
        self._pending = {token: pending}
        return _err(
            "confirmation_required",
            prompt,
            token=token,
            service=call.service_id,
            entity=name,
            danger=action,
            expires_in=_CONFIRM_TTL,
        )

    def _prune_pending(self) -> None:
        now = time.time()
        self._pending = {token: item for token, item in self._pending.items() if not item.expired(now)}

    async def _handle_pending_reply(self, command: str) -> Any:
        """处理对确认提示的答复；不是答复时返回 None 让正常解析继续。"""
        if not self._pending:
            return None
        self._prune_pending()
        if not self._pending:
            return None

        if is_denial(command):
            self._pending.clear()
            return Ok(
                {
                    "success": True,
                    "cancelled": True,
                    "message": self._t("messages.cancelled", "已取消，没有执行任何操作"),
                }
            )

        if is_confirmation(command):
            # 只取最新一条；多条并存本身就不该发生（签发时会覆盖）
            pending = max(self._pending.values(), key=lambda item: item.expires_at)
            self._pending.clear()
            entity = {
                "entity_id": pending.entity_id,
                "name": pending.entity_name,
                "caps": {},
            }
            self.logger.info(f"确认执行危险动作: {pending.domain}.{pending.service} → {pending.entity_id}")
            return await self._perform(pending.to_call(), entity, allow_dangerous=True)

        return None


