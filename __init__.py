"""N.E.K.O 智能家居插件：通过 Home Assistant 控制全平台设备。

## 架构

    你说的话 ──▶ NEKO 的 LLM（function calling）
                     │  find_devices / get_state / control_devices / run_scene
                     ▼
              工具层（本文件）──▶ ha_api（caps 能力归一化 + REST/WS）
                     │
                     ▼
              Home Assistant REST / WebSocket

**本插件不做自然语言解析。** 理解「客厅灯调暗点」这件事交给 NEKO 自己的 LLM，
它本来就擅长把一句话拆成 `targets` + `action` + `params`。插件只负责三件事：
把设备索引暴露清楚、把动作翻译成 HA 服务、把危险动作拦住。

这样做的代价是模糊表达依赖模型，收益是插件里不再有第二套中文分词规则要养。

## 工具面（5 个）

    find_devices(area?, domain?, name?)        找设备，返回可否控制与风险档位
    get_state(targets[])                       批量查状态
    control_devices(targets[], action, params?) 批量控制，逐台返回结果
    run_scene(name)                            执行场景 / 脚本
    ask_home(text)                             兜底，转给 HA 内置 Assist

配置与查询类走 `@plugin_entry`（面板 / analyzer / `/runs`），控制类走
`@llm_tool`（对话期由 LLM 调用）。两者**不能混挂**：宿主会把 analyzer 视野里
的 `__llm_tool__*` 剥掉。

## 安全

* 白名单按风险三档自动归类，高危实体默认不可控，只能逐个授权（`security.py`）
* 高危动作两阶段：先返回确认要求，带一次性令牌再执行
* 执行后回读状态，确认真的变了
* 审计日志只记动作与结果，绝不记令牌
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
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

from . import secret_store
from .ha_api import AsyncHaClient, ConnectionInfo, HaAuthError, HaConnectionError, HaError, HaServiceError
from .ha_api.caps import CONTROLLABLE_DOMAINS, is_read_only, is_supported
from .ha_api.client import EntitySnapshot, probe_connection
from .ha_api.entities import state_facts
from .ha_api.services import ACTIONS, ServiceCall, build_call, scene_call
from .resolver import AMBIGUOUS, NOT_FOUND, resolve_targets
from .security import AuditLog, ConfirmStore, Whitelist

# 实体缓存 schema 版本。caps 结构变化时必须 +1，否则旧缓存会让校验读不到新字段。
_ENTITIES_CACHE_SCHEMA = 2
# 内存快照有效期。HA 的状态变化对语音控制不敏感，60 秒足够。
_SNAPSHOT_TTL = 60.0
# 一次工具调用最多返回多少台设备，超出置 truncated。
_LIST_LIMIT = 50
# 一次 control_devices 最多对多少台做回读校验。太多会把一次对话拖成几十个请求。
_READBACK_LIMIT = 20

_NOT_CONFIGURED_HINT = (
    "还没有连接 Home Assistant。请在插件面板填写 HA 地址（默认已填 http://127.0.0.1:8123）"
    "与长期访问令牌，然后点「保存并连接」。"
)

# 面板输入框的预填值，也是后端在调用方没给地址时的兜底。
# HA 与 N.E.K.O 跑在同一台机器是最常见的试用场景，写死本机地址能让这类用户
# 只需粘贴令牌、点一下保存就接通；HA 在别的机器上改一下 IP 即可。
DEFAULT_BASE_URL = "http://127.0.0.1:8123"

_CONFIRM_HINT = (
    "这是高危动作，尚未执行。请把 summary 中的设备、动作与参数转述给用户，"
    "并请用户回复 confirmation.token。拿到用户确认后，用**完全相同的** "
    "targets / action / params 再调一次，并把该令牌填进 confirm_token。"
    "用户若表示取消，就不要重试。"
)

# 动作执行后，状态应该变成什么。用于回读校验"真的变了没有"。
_EXPECTED_STATES: dict[str, set[str]] = {
    "turn_on": {"on"},
    "turn_off": {"off"},
    "open": {"open", "opening"},
    "close": {"closed", "closing"},
    "lock": {"locked", "locking"},
    "unlock": {"unlocked", "unlocking"},
    "disarm": {"disarmed"},
    "arm_home": {"armed_home", "arming"},
    "arm_away": {"armed_away", "arming"},
    "arm_night": {"armed_night", "arming"},
    "play": {"playing"},
    "pause": {"paused"},
}


def _open_url_in_browser(url: str) -> None:
    """在系统默认浏览器中打开 URL（同步，通过 to_thread 调用）。"""
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


def _err(code: str, message: str, **detail: Any) -> Any:
    """构造带稳定错误码 + 结构化 detail 的内部 Err。"""
    return Err(SdkError(message, code=code, details=detail or None))


def _tool_payload(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """@llm_tool 的返回信封。统一入口，避免各处手写 is_error 拼错。"""
    return {"output": payload, "is_error": is_error}


def _tool_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """失败信封：``output`` 里同时给稳定错误码、结构化细节与人话。

    细节**平铺**在 output 上，不再多套一层 ``detail``——少一层嵌套，模型漏读
    的概率更低，同一条信息也不必在两处各写一遍。
    """
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return {"output": payload, "is_error": True, "error": code}


def _capabilities(entity: dict[str, Any]) -> list[str]:
    """实体支持的能力名，给 LLM 判断 params 用。"""
    caps = entity.get("caps") or {}
    return sorted(key for key in caps if key not in ("risk", "device_class", "dangerous"))


def _expected_state(action: str, domain: str) -> set[str] | None:
    """回读时用来判定"真的变了"的期望状态；None 表示无法据此判断。"""
    if action == "turn_on" and domain == "media_player":
        return None  # 影音"开"之后可能是 idle/playing/paused，不据 state 判定
    return _EXPECTED_STATES.get(action)


# ── 工具 schema ─────────────────────────────────────────────────────────
FIND_DEVICES_DESCRIPTION = (
    "在用户的 Home Assistant 里查找设备。用户提到某个设备但你不确定它的确切名称、"
    "或不确定自己有没有权限控制它时，先用这个工具查。\n\n"
    "不要用它查询设备当前状态（那是 get_state），也不要用它执行任何动作。\n"
    "返回每台设备的 entity_id、名称、区域、domain、风险档位、是否可控制，"
    "以及它支持哪些能力（brightness / color_temp / temperature / position / volume …），"
    "这些能力名就是 control_devices 的 params 键。\n"
    "只读设备（传感器、天气等）会带 read_only=true：不能控制，但可以用 get_state 查询。\n"
    "结果超过上限时 truncated=true，请用 area/domain/name 缩小范围后重查。"
)

FIND_DEVICES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "area": {"type": "string", "description": "区域（房间）名，如 客厅。留空不限"},
        "domain": {
            "type": "string",
            "description": (
                "设备类型：light / switch / fan / cover / climate / media_player / "
                "scene / script / lock / alarm_control_panel / valve / water_heater"
            ),
        },
        "name": {"type": "string", "description": "设备名或别名的一部分，如 吸顶灯"},
        "include_blocked": {
            "type": "boolean",
            "description": "是否也返回用户未授权控制的设备（默认 false）",
        },
        "limit": {"type": "integer", "description": "最多返回多少台，默认 50"},
    },
    "required": [],
}

GET_STATE_DESCRIPTION = (
    "批量查询设备的当前状态。用户问「客厅灯开着吗」「空调现在几度」「窗帘开了一半没有」时用。\n\n"
    "targets 可以是 entity_id、设备名、别名或区域名，插件会自行匹配。\n"
    "匹配不到会返回 status=not_found；匹配到多个会返回 status=ambiguous 并给出候选，"
    "这时请把候选念给用户让他选，不要自己挑一个。\n"
    "这是只读操作，不会改变任何设备。"
)

GET_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "targets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "要查询的设备，如 [\"客厅吸顶灯\", \"空调\"]",
        },
    },
    "required": ["targets"],
}

CONTROL_DEVICES_DESCRIPTION = (
    "控制一个或多个 Home Assistant 设备。**这是唯一会改变物理世界的工具。**\n\n"
    "先想清楚三件事再调用：\n"
    "  1. targets —— 用户说的设备名原样传，不要自己翻译成 entity_id（除非你已经"
    "用 find_devices 查到确切 entity_id）。\n"
    "  2. action —— 从枚举里选，不要自创。开灯用 turn_on，不是 set。\n"
    "  3. params —— 只有用户明确说了数值/模式才传；只说「亮一点」这种模糊量时，"
    "不要自己编一个数字，先问用户要调到多少。\n\n"
    "一次可以控多台，逐台返回结果：status 为 ok / not_found / ambiguous / blocked / "
    "unsupported / unsupported_params / failed。**必须如实转述每一台的失败原因**，"
    "不要说成全部成功。\n\n"
    "⚠️ 高危动作（解锁、开车库门/大门、开阀门、撤防、给热水器加热）第一轮不会执行，"
    "而是返回 status=confirmation_required 与 confirmation.token。此时**不要**重试或"
    "改参数绕过：把设备、动作、参数转述给用户，等用户回复令牌后再用完全相同的参数"
    "调一次，并填 confirm_token。\n\n"
    "执行成功后会回读设备状态并给出 verified：true 表示状态确实变了，false 表示"
    "命令发出去了但设备没反应（通常是离线或物理卡住），这时要如实告诉用户。"
)

CONTROL_DEVICES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "targets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "目标设备名 / 别名 / entity_id / 区域名，可多个",
        },
        "action": {"type": "string", "enum": list(ACTIONS), "description": "要执行的动作"},
        "params": {
            "type": "object",
            "description": (
                "动作参数，键必须在该设备的能力列表里。常用："
                "brightness 0-100、color_temp 开尔文、color #RRGGBB、"
                "temperature 摄氏度、hvac_mode、fan_speed、position 0-100、"
                "volume 0-100、percentage 0-100、source、preset、operation"
            ),
        },
        "confirm_token": {
            "type": "string",
            "description": "高危动作的用户确认令牌。只有上一轮返回 confirmation_required 时才传",
        },
    },
    "required": ["targets", "action"],
}

RUN_SCENE_DESCRIPTION = (
    "执行 Home Assistant 里的场景（scene）或脚本（script）。用户说「执行回家场景」"
    "「运行晚安脚本」时用。\n\n"
    "只需要场景名，插件会自行匹配。匹配不到会返回候选列表。\n"
    "注意：场景没有「关闭」的概念，不要试图用这个工具关闭场景。"
)

RUN_SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string", "description": "场景或脚本名，如 回家"}},
    "required": ["name"],
}

ASK_HOME_DESCRIPTION = (
    "兜底工具：把一句话原样交给 Home Assistant 自己的对话代理（Assist）处理。\n\n"
    "什么时候用：用户的话你判断是智能家居相关，但 target / action 都抽不出来，"
    "或者包含本插件没覆盖的 domain（扫地机、加湿器、摄像头等）。\n\n"
    "⚠️ 不要把它当主力。HA 内置中文 intent 的覆盖度没有保证，经常返回"
    "「没有匹配的意图」。失败时请如实告诉用户这条路不通，不要假装成功。"
)

ASK_HOME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "用户原话"},
        "language": {"type": "string", "description": "语言代码，如 zh-CN。留空用 HA 默认"},
    },
    "required": ["text"],
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
        # 白名单与确认令牌。两者都不落盘到"可被 LLM 读到"的地方。
        self._whitelist = Whitelist()
        self._confirms = ConfirmStore()
        self._audit = AuditLog(self._data_path("audit.jsonl"))
        # 令牌是否真的落盘了。加密不可用时为 False——必须让用户知道重启要重填。
        self._token_persisted: bool = False

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
            await self._load_whitelist()
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
        """插件关闭：取消后台任务、关闭 HTTP 连接、清空待确认动作。"""
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
        self._confirms.clear()
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        """配置变化时重新加载连接。"""
        await self._reload_connection()
        return Ok({"reloaded": self.client is not None})

    # ========== 连接与凭据 ==========
    async def _load_connection(self) -> ConnectionInfo:
        """读凭据。三条通道依次尝试，最后才看历史遗留的明文文件。

        顺序：加密文件（DPAPI）→ 系统钥匙串 → 明文文件（迁移后删除）。
        明文分支只为兼容 v1.0.0 落下的 `credential.json`，读到就立刻转存加密
        并删掉明文。
        """
        blob_path = self._data_path("credential.bin")
        try:
            if blob_path.exists():
                blob = await asyncio.to_thread(blob_path.read_bytes)
                raw_bytes = await asyncio.to_thread(secret_store.decrypt, blob)
                if raw_bytes:
                    self._token_persisted = True
                    return ConnectionInfo.from_dict(json.loads(raw_bytes.decode("utf-8")))
        except Exception as exc:
            self.logger.warning(f"读取加密凭据失败：{exc}")

        try:
            token = await asyncio.to_thread(secret_store.keyring_get)
            meta_path = self._data_path("connection.json")
            if token and meta_path.exists():
                raw = await read_json_async(meta_path)
                info = ConnectionInfo.from_dict(raw)
                info.token = token
                self._token_persisted = True
                return info
        except Exception as exc:
            self.logger.debug(f"读取钥匙串失败：{exc}")

        legacy = self._data_path("credential.json")
        if legacy.exists():
            try:
                raw = await read_json_async(legacy)
                info = ConnectionInfo.from_dict(raw)
                if info.token:
                    # 迁移：立刻转成加密存储，然后删掉明文。
                    await self._save_connection(info)
                    try:
                        await asyncio.to_thread(legacy.unlink)
                    except OSError:
                        pass
                    self.logger.info("已把明文令牌迁移到加密存储")
                return info
            except Exception as exc:
                self.logger.warning(f"读取旧凭据失败：{exc}")

        self._token_persisted = False
        return ConnectionInfo()

    async def _save_connection(self, connection: ConnectionInfo) -> None:
        """保存凭据：能加密就加密，不能就只放内存——**绝不写明文**。"""
        payload = json.dumps(connection.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        blob = await asyncio.to_thread(secret_store.encrypt, payload)

        if blob is not None:
            path = self._data_path("credential.bin")
            await asyncio.to_thread(self._atomic_write_bytes, path, blob)
            self._token_persisted = True
            await self._drop_legacy_secret()
            return

        if await asyncio.to_thread(secret_store.keyring_set, connection.token):
            meta = connection.to_dict()
            meta["token"] = ""
            await atomic_write_json_async(self._data_path("connection.json"), meta)
            self._token_persisted = True
            await self._drop_legacy_secret()
            return

        # 两条通道都不可用：令牌只留在 self.connection 里，重启即失效。
        self._token_persisted = False
        self.logger.warning("系统没有可用的加密后端，令牌只保存在进程内存中（重启需重新填写）")

    async def _drop_legacy_secret(self) -> None:
        for name in ("credential.json", "credential.bin.tmp"):
            path = self._data_path(name)
            try:
                if path.exists():
                    await asyncio.to_thread(path.unlink)
            except OSError:
                pass

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """原子写敏感文件：临时文件 → 收紧权限 → 写内容 → replace。

        直接写有两个问题：写入中途崩溃会留下半截文件；文件在默认权限下先落地，
        存在一个短暂的"任何用户可读"窗口。令牌就是凭据，这个窗口不能留。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_cred_", suffix=".bin")
        try:
            if sys.platform != "win32":
                os.chmod(tmp_name, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            HomeAssistantPlugin._harden_permissions(path)
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
        await atomic_write_json_async(self._data_path("aliases.json"), {"aliases": self._aliases})

    # ========== 白名单 ==========
    async def _load_whitelist(self) -> None:
        try:
            raw = await read_json_async(self._data_path("whitelist.json"))
        except Exception:
            self._whitelist = Whitelist()
            return
        self._whitelist = Whitelist.from_dict(raw)

    async def _save_whitelist(self) -> None:
        await atomic_write_json_async(self._data_path("whitelist.json"), self._whitelist.to_dict())

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
            await atomic_write_json_async(self._data_path("entities_cache.json"), payload)
        except Exception as exc:
            # 缓存写失败不影响控制，只是下次要重新拉。
            self.logger.debug(f"写入实体缓存失败：{exc}")

    def _sync_snapshot_state(self, entity_id: str, state: str, attributes: dict[str, Any] | None) -> None:
        """把回读到的状态写回内存快照，让后续查询不必再打一次网络。"""
        if self._snapshot is None:
            return
        for entity in self._snapshot.entities:
            if entity.get("entity_id") == entity_id:
                entity["state"] = state
                entity["is_online"] = state not in ("unavailable", "unknown")
                if attributes is not None:
                    entity["attributes"] = attributes
                break

    # ========== UI 上下文 ==========
    @ui.context(id="dashboard", title="Home Assistant 智能家居")
    async def get_dashboard_context(self):
        """为 Hosted UI 面板提供状态数据。"""
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

        return {
            "configured": self.connection.configured,
            "base_url": self.connection.base_url,
            "location_name": self.connection.location_name,
            "ha_version": self.connection.ha_version,
            "degraded": degraded,
            "area_count": area_count,
            "entity_count": len(entities),
            "entities": self._decorate(entities),
            "domain_counts": self._domain_counts(entities),
            "secret_backend": secret_store.backend(),
            "token_persisted": self._token_persisted,
        }

    def _decorate(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给面板用的实体视图：补上是否可控、原因、是否只读。"""
        out: list[dict[str, Any]] = []
        for entity in entities:
            allowed, reason = self._whitelist.decision(entity)
            item = dict(entity)
            item["allowed"] = allowed
            item["allowed_reason"] = reason
            item["read_only"] = is_read_only(str(entity.get("domain") or ""))
            out.append(item)
        return out

    @staticmethod
    def _domain_counts(entities: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entity in entities:
            domain = entity.get("domain", "")
            counts[domain] = counts.get(domain, 0) + 1
        return counts

    # ========== 配置类入口（面板 / analyzer）==========
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
        base_url = (base_url or "").strip() or DEFAULT_BASE_URL
        if not token:
            return _err("missing_parameter", "请填写长期访问令牌")
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
        base_url = (base_url or "").strip() or DEFAULT_BASE_URL
        if not token:
            return _err("missing_parameter", "请填写长期访问令牌")
        try:
            info = await probe_connection(base_url, token)
        except HaAuthError as exc:
            return _err("token_expired", str(exc))
        except HaConnectionError as exc:
            return _err("ha_unreachable", str(exc))
        except Exception as exc:
            return _err("connection_failed", f"连接失败：{exc}")

        await self._save_connection(info)
        self.connection = info
        if self.client is not None:
            await self.client.close()
        self.client = AsyncHaClient(info)
        self._invalidate_snapshot()
        self.logger.info(f"已保存 Home Assistant 连接：{info.base_url}")
        message = f"已连接：{info.location_name or 'Home Assistant'}"
        if not self._token_persisted:
            message += "（当前系统没有可用的加密后端，令牌只保存在内存中，重启后需要重新填写）"
        return Ok(
            {
                "success": True,
                "message": message,
                "base_url": info.base_url,
                "ha_version": info.ha_version,
                "token_persisted": self._token_persisted,
                "secret_backend": secret_store.backend(),
            }
        )

    @plugin_entry(
        id="get_status",
        name=tr("entries.get_status.name", default="查看连接状态"),
        description=tr("entries.get_status.description", default="返回当前连接信息与设备统计，供面板首屏渲染"),
    )
    async def get_status(self, **_):
        """面板首屏用：连接信息（令牌脱敏）+ 缓存统计 + 安全状态。

        刻意不触发网络请求——面板打开就卡住等 HA 响应是最糟的体验。
        需要最新数据时面板会另外调 reload_entities。
        """
        snapshot = self._snapshot or await self._read_snapshot_cache()
        entities = snapshot.entities if snapshot else []
        allowed = sum(1 for entity in entities if self._whitelist.allows(entity))
        pending = self._confirms.peek()
        return Ok(
            {
                "success": True,
                "connection": self.connection.public(),
                "entity_count": len(entities),
                "controllable_count": allowed,
                "high_risk_count": sum(1 for entity in entities if entity.get("risk") == "high"),
                "area_count": snapshot.area_count if snapshot else 0,
                "degraded": snapshot.degraded if snapshot else "",
                "entities": self._decorate(entities),
                "token_persisted": self._token_persisted,
                "secret_backend": secret_store.backend(),
                "pending_confirmation": pending.public() if pending else None,
            }
        )

    @plugin_entry(
        id="clear_connection",
        name=tr("entries.clear_connection.name", default="断开连接"),
        description=tr("entries.clear_connection.description", default="删除保存的地址与令牌，并清空实体缓存"),
        kind="action",
    )
    async def clear_connection(self, **_):
        """断开连接并清空本地数据（含白名单与待确认动作）。"""
        for name in ("credential.json", "credential.bin", "connection.json", "entities_cache.json", "aliases.json", "whitelist.json"):
            path = self._data_path(name)
            try:
                if path.exists():
                    await asyncio.to_thread(path.unlink)
            except Exception as exc:
                self.logger.warning(f"删除 {name} 失败：{exc}")
        try:
            await asyncio.to_thread(secret_store.keyring_delete)
        except Exception:
            pass

        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
        self.client = None
        self.connection = ConnectionInfo()
        self._aliases = {}
        self._whitelist = Whitelist()
        self._confirms.clear()
        self._token_persisted = False
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
        description=tr("entries.list_entities.description", default="列出实体，可按 domain 或区域过滤，并标注可否控制"),
        input_schema={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "如 light / climate / cover，留空为全部"},
                "area": {"type": "string", "description": "区域名，留空为全部"},
                "only_allowed": {"type": "boolean", "description": "只列出已授权控制的设备"},
            },
            "required": [],
        },
        llm_result_fields=["message"],
    )
    async def list_entities(self, domain: str = "", area: str = "", only_allowed: bool = False, **_):
        """列出实体。"""
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
        if only_allowed:
            items = [item for item in items if self._whitelist.allows(item)]

        lines = [f"共 {len(items)} 个设备："]
        for item in items[:60]:
            state = item.get("state", "")
            label = f"{item.get('area_name', '')} {item.get('name', '')}".strip()
            mark = "" if self._whitelist.allows(item) else "（未授权控制）"
            lines.append(f"  • {label}{mark} — {state}")
        if len(items) > 60:
            lines.append(f"  …另有 {len(items) - 60} 个")

        return Ok(
            {
                "success": True,
                "message": "\n".join(lines),
                "entities": self._decorate(items[:60]),
                "count": len(items),
                "truncated": len(items) > 60,
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
        description=tr("entries.set_entity_alias.description", default="给实体设一个好叫的别名，留空则删除"),
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
        id="set_entity_allowed",
        name=tr("entries.set_entity_allowed.name", default="授权/取消控制某个设备"),
        description=tr(
            "entries.set_entity_allowed.description",
            default="高危设备（门锁、车库门、阀门、热水器等）默认不可控，用这个入口逐个授权",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "如 lock.front_door"},
                "allowed": {"type": "boolean", "description": "true 允许控制，false 收回"},
            },
            "required": ["entity_id", "allowed"],
        },
        llm_result_fields=["message"],
    )
    async def set_entity_allowed(self, entity_id: str = "", allowed: bool = True, **_):
        """逐个授权。高危设备只能走这一条路，不提供整类一键放行。"""
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            return _err("missing_parameter", "请提供 entity_id")

        # 主动拉一次实体表来校验：给传感器/扫地机授权是没意义的，静默存下来
        # 只会在面板上显示一个永远不生效的勾。这里只在用户点一下时发生，
        # 一次网络往返可以接受。
        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)
        try:
            snapshot = await self._get_snapshot()
        except Exception as exc:
            return _err("entity_list_unavailable", f"获取设备列表失败：{exc}")

        entity = next((item for item in snapshot.entities if item.get("entity_id") == entity_id), None)
        if entity is None:
            return _err("entity_not_found", f"没找到设备 {entity_id}")
        if not is_supported(str(entity.get("domain") or "")):
            return _err(
                "unsupported_domain",
                f"{entity.get('domain', '')} 类设备不支持控制，无需授权",
                domain=str(entity.get("domain") or ""),
            )

        self._whitelist.set_entity(entity_id, bool(allowed))
        await self._save_whitelist()
        action = "允许" if allowed else "禁止"
        return Ok(
            {
                "success": True,
                "message": f"已{action}控制 {entity_id}",
                "entity_id": entity_id,
                "allowed": bool(allowed),
            }
        )

    @plugin_entry(
        id="get_whitelist",
        name=tr("entries.get_whitelist.name", default="查看控制白名单"),
        description=tr("entries.get_whitelist.description", default="返回哪些设备可被控制，以及每个设备的授权原因"),
        llm_result_fields=["message"],
    )
    async def get_whitelist(self, **_):
        snapshot = self._snapshot or await self._read_snapshot_cache()
        entities = snapshot.entities if snapshot else []
        rows = self._decorate(entities)
        allowed = [row for row in rows if row["allowed"]]
        blocked = [row for row in rows if not row["allowed"]]
        return Ok(
            {
                "success": True,
                "message": f"可控制 {len(allowed)} 个，未授权 {len(blocked)} 个",
                "overrides": dict(self._whitelist.overrides),
                "allowed_count": len(allowed),
                "blocked_count": len(blocked),
                "entities": rows,
            }
        )

    @plugin_entry(
        id="query_entity_state",
        name=tr("entries.query_entity_state.name", default="查询设备状态"),
        description=tr("entries.query_entity_state.description", default="查询某个设备当前的状态，如温度、亮度、开关"),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "设备名或别名,如 客厅灯"}},
            "required": ["name"],
        },
        llm_result_fields=["message"],
    )
    async def query_entity_state(self, name: str = "", **_):
        """查询状态。走与 get_state 工具相同的解析路径，避免两套匹配逻辑。"""
        if self.client is None:
            return _err("not_configured", _NOT_CONFIGURED_HINT)
        if not name:
            return _err("missing_parameter", "请提供要查询的设备名")
        try:
            snapshot = await self._get_snapshot()
        except Exception as exc:
            return _err("entity_list_unavailable", f"获取设备列表失败：{exc}")

        match = resolve_targets([name], snapshot.entities)[0]
        if match.status == NOT_FOUND:
            return _err("entity_not_found", f"没找到「{name}」")
        if match.status == AMBIGUOUS:
            return _err(
                "entity_ambiguous",
                f"「{name}」匹配到多个设备，请说得更具体",
                candidates=match.candidate_briefs(),
            )
        entity = match.entity or {}
        facts = state_facts(entity)
        return Ok(
            {
                "success": True,
                "message": self._describe(entity, facts),
                "entity_id": entity.get("entity_id", ""),
                "name": entity.get("name", ""),
                "state": entity.get("state", ""),
                "facts": facts,
                "attributes": entity.get("attributes", {}),
            }
        )

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
                "entities": self._decorate(snapshot.entities),
                "count": len(snapshot.entities),
                "degraded": snapshot.degraded,
            }
        )

    # ========== 对话工具 ==========
    @llm_tool(
        name="find_devices",
        description=FIND_DEVICES_DESCRIPTION,
        parameters=FIND_DEVICES_SCHEMA,
        timeout=30.0,
    )
    async def find_devices(
        self,
        *,
        area: str = "",
        domain: str = "",
        name: str = "",
        include_blocked: bool = False,
        limit: int = _LIST_LIMIT,
        **_,
    ) -> dict[str, Any]:
        """按区域 / 类型 / 名字查设备。"""
        if self.client is None:
            return _tool_error("not_configured", _NOT_CONFIGURED_HINT)
        try:
            snapshot = await self._get_snapshot()
        except HaAuthError as exc:
            return _tool_error("token_expired", str(exc))
        except HaConnectionError as exc:
            return _tool_error("ha_unreachable", str(exc))
        except Exception as exc:
            return _tool_error("entity_list_unavailable", f"获取设备列表失败：{exc}")

        items = snapshot.entities
        if domain:
            items = [item for item in items if item.get("domain") == str(domain).strip()]
        if area:
            needle = str(area).strip().lower()
            items = [item for item in items if needle in str(item.get("area_name", "")).lower()]
        if name:
            needle = str(name).strip().lower()
            items = [
                item
                for item in items
                if needle in str(item.get("name", "")).lower() or needle in str(item.get("alias", "")).lower()
            ]

        rows = []
        for item in items:
            allowed, reason = self._whitelist.decision(item)
            # 只读实体（传感器、天气、人员）不可控但**可以查**，不能因为白名单
            # 拦了就把它从结果里抹掉——否则模型永远发现不了「客厅温度」。
            read_only = is_read_only(str(item.get("domain") or ""))
            if not allowed and not include_blocked and not read_only:
                continue
            rows.append(
                {
                    "entity_id": item.get("entity_id", ""),
                    "name": item.get("name", ""),
                    "alias": item.get("alias", ""),
                    "area": item.get("area_name", ""),
                    "domain": item.get("domain", ""),
                    "device_class": item.get("device_class", ""),
                    "state": item.get("state", ""),
                    "risk": item.get("risk", ""),
                    "controllable": allowed,
                    "read_only": read_only,
                    "blocked_reason": "" if allowed else reason,
                    "capabilities": _capabilities(item),
                }
            )

        cap = max(1, min(int(limit or _LIST_LIMIT), 200))
        total = len(rows)
        return _tool_payload(
            {
                "devices": rows[:cap],
                "total": total,
                "truncated": total > cap,
                "degraded": snapshot.degraded,
                "supported_domains": sorted(CONTROLLABLE_DOMAINS),
            }
        )

    @llm_tool(
        name="get_state",
        description=GET_STATE_DESCRIPTION,
        parameters=GET_STATE_SCHEMA,
        timeout=30.0,
    )
    async def get_state(self, *, targets: Any = None, **_) -> dict[str, Any]:
        """批量查询状态。"""
        if self.client is None:
            return _tool_error("not_configured", _NOT_CONFIGURED_HINT)
        target_list = self._as_targets(targets)
        if not target_list:
            return _tool_error("missing_parameter", "targets 不能为空")

        try:
            snapshot = await self._get_snapshot()
        except Exception as exc:
            return _tool_error("entity_list_unavailable", f"获取设备列表失败：{exc}")

        results: list[dict[str, Any]] = []
        for match in resolve_targets(target_list, snapshot.entities):
            if match.status == NOT_FOUND:
                results.append({"target": match.target, "status": NOT_FOUND})
                continue
            if match.status == AMBIGUOUS:
                results.append(
                    {"target": match.target, "status": AMBIGUOUS, "candidates": match.candidate_briefs()}
                )
                continue
            group = match.candidates or ([match.entity] if match.entity else [])
            for entity in group:
                facts = state_facts(entity)
                results.append(
                    {
                        "target": match.target,
                        "status": "ok",
                        "entity_id": entity.get("entity_id", ""),
                        "name": entity.get("name", ""),
                        "area": entity.get("area_name", ""),
                        "domain": entity.get("domain", ""),
                        "state": entity.get("state", ""),
                        "facts": facts,
                        "attributes": entity.get("attributes", {}),
                    }
                )

        total = len(results)
        return _tool_payload({"results": results[:_LIST_LIMIT], "total": total, "truncated": total > _LIST_LIMIT})

    @llm_tool(
        name="control_devices",
        description=CONTROL_DEVICES_DESCRIPTION,
        parameters=CONTROL_DEVICES_SCHEMA,
        # HA 的服务调用会同步等待动作完成；批量控制留足余量。
        timeout=60.0,
    )
    async def control_devices(
        self,
        *,
        targets: Any = None,
        action: str = "",
        params: Any = None,
        confirm_token: str = "",
        **_,
    ) -> dict[str, Any]:
        """批量控制。逐台返回结果，高危动作两阶段确认。"""
        if self.client is None:
            return _tool_error("not_configured", _NOT_CONFIGURED_HINT)

        act = str(action or "").strip().lower()
        if act not in ACTIONS:
            return _tool_error(
                "invalid_action",
                f"不支持的动作「{action}」",
                allowed=list(ACTIONS),
            )
        target_list = self._as_targets(targets)
        if not target_list:
            return _tool_error("missing_parameter", "targets 不能为空")
        param_map = params if isinstance(params, dict) else {}

        try:
            snapshot = await self._get_snapshot()
        except HaAuthError as exc:
            return _tool_error("token_expired", str(exc))
        except HaConnectionError as exc:
            return _tool_error("ha_unreachable", str(exc))
        except Exception as exc:
            return _tool_error("entity_list_unavailable", f"获取设备列表失败：{exc}")

        matches = resolve_targets(target_list, snapshot.entities)
        results: list[dict[str, Any]] = []
        planned: list[tuple[dict[str, Any], ServiceCall]] = []
        seen: set[str] = set()

        for match in matches:
            if match.status == NOT_FOUND:
                results.append({"target": match.target, "status": NOT_FOUND})
                continue
            if match.status == AMBIGUOUS:
                results.append(
                    {"target": match.target, "status": AMBIGUOUS, "candidates": match.candidate_briefs()}
                )
                continue
            for entity in match.candidates or ([match.entity] if match.entity else []):
                entity_id = str(entity.get("entity_id", ""))
                if not entity_id or entity_id in seen:
                    continue
                seen.add(entity_id)
                self._plan(entity, match.target, act, param_map, results, planned)

        if not planned:
            return _tool_payload(
                {"results": results, "ok": False, "executed": 0, "blocked": len(results)},
                is_error=all(item["status"] != "ok" for item in results),
            )

        entity_ids = [str(entity.get("entity_id", "")) for entity, _call in planned]
        dangerous = [(entity, call) for entity, call in planned if call.dangerous]

        if dangerous:
            if not confirm_token:
                pending = self._confirms.issue(entity_ids, act, param_map, targets=target_list)
                for entity, call in planned:
                    results.append(
                        {
                            "target": entity.get("name", ""),
                            "entity_id": entity.get("entity_id", ""),
                            "status": "confirmation_required",
                            "action": act,
                            "params": param_map,
                            "risk": entity.get("risk", ""),
                            "requires_confirmation": True,
                            "service": call.service_id,
                        }
                    )
                self._audit.record(
                    action=act,
                    targets=entity_ids,
                    result="confirmation_required",
                    risk="high",
                )
                return _tool_error(
                    "confirmation_required",
                    _CONFIRM_HINT,
                    confirmation=pending.public(),
                    results=results,
                )

            reason = self._confirms.consume(confirm_token, entity_ids, act, param_map)
            if reason != "ok":
                for entity, call in planned:
                    results.append(
                        {
                            "target": entity.get("name", ""),
                            "entity_id": entity.get("entity_id", ""),
                            "status": "confirmation_invalid",
                            "reason": reason,
                            "action": act,
                        }
                    )
                return _tool_error(
                    "confirmation_invalid",
                    "确认令牌无效或已过期，动作未执行",
                    reason=reason,
                    results=results,
                )

        successes = 0
        for entity, call in planned:
            item = {
                "target": entity.get("name", ""),
                "entity_id": entity.get("entity_id", ""),
                "action": act,
                "params": param_map,
                "service": call.service_id,
                "risk": entity.get("risk", ""),
                "state_before": entity.get("state", ""),
            }
            try:
                await self.client.call(call)
            except HaAuthError as exc:
                item.update({"status": "failed", "code": "token_expired", "message": str(exc)})
            except HaConnectionError as exc:
                item.update({"status": "failed", "code": "ha_unreachable", "message": str(exc)})
            except HaServiceError as exc:
                item.update({"status": "failed", "code": "service_failed", "message": str(exc)})
            except Exception as exc:
                item.update({"status": "failed", "code": "control_failed", "message": str(exc)})
            else:
                item["status"] = "ok"
                successes += 1
            results.append(item)
            if item["status"] == "ok":
                self._audit.record(
                    action=act,
                    targets=[item["entity_id"]],
                    result="ok",
                    risk=str(entity.get("risk", "")),
                )
            else:
                self._audit.record(
                    action=act,
                    targets=[item["entity_id"]],
                    result=item.get("code", "failed"),
                    risk=str(entity.get("risk", "")),
                )

        await self._readback(results, act)

        ok_count = sum(1 for item in results if item["status"] == "ok")
        all_ok = ok_count == len(results)
        return _tool_payload(
            {
                "results": results,
                "ok": all_ok,
                "executed": successes,
                "blocked": sum(1 for item in results if item["status"] not in ("ok", "failed")),
            },
            is_error=not all_ok,
        )

    @llm_tool(
        name="run_scene",
        description=RUN_SCENE_DESCRIPTION,
        parameters=RUN_SCENE_SCHEMA,
        timeout=30.0,
    )
    async def run_scene(self, *, name: str = "", **_) -> dict[str, Any]:
        """执行场景或脚本。"""
        if self.client is None:
            return _tool_error("not_configured", _NOT_CONFIGURED_HINT)
        needle = str(name or "").strip()
        if not needle:
            return _tool_error("missing_parameter", "场景名不能为空")

        try:
            snapshot = await self._get_snapshot()
        except Exception as exc:
            return _tool_error("entity_list_unavailable", f"获取设备列表失败：{exc}")

        candidates = [item for item in snapshot.entities if item.get("domain") in ("scene", "script")]
        match = resolve_targets([needle], candidates)[0]
        if match.status == NOT_FOUND:
            return _tool_error(
                "scene_not_found",
                f"没找到场景「{needle}」",
                available=[item.get("name", "") for item in candidates[:_LIST_LIMIT]],
            )
        if match.status == AMBIGUOUS:
            return _tool_error(
                "scene_ambiguous",
                f"「{needle}」匹配到多个场景，请说得更具体",
                candidates=match.candidate_briefs(),
            )

        entity = match.entity or {}
        call = scene_call(entity)
        try:
            await self.client.call(call)
        except HaAuthError as exc:
            return _tool_error("token_expired", str(exc))
        except HaConnectionError as exc:
            return _tool_error("ha_unreachable", str(exc))
        except HaServiceError as exc:
            return _tool_error("service_failed", str(exc), service=call.service_id)
        except Exception as exc:
            return _tool_error("control_failed", f"执行场景失败：{exc}")

        self._audit.record(
            action="run",
            targets=[entity.get("entity_id", "")],
            result="ok",
            risk=str(entity.get("risk", "")),
        )
        return _tool_payload(
            {
                "results": [
                    {
                        "status": "ok",
                        "entity_id": entity.get("entity_id", ""),
                        "name": entity.get("name", ""),
                        "domain": entity.get("domain", ""),
                        "service": call.service_id,
                    }
                ],
                "ok": True,
                "executed": 1,
            }
        )

    @llm_tool(
        name="ask_home",
        description=ASK_HOME_DESCRIPTION,
        parameters=ASK_HOME_SCHEMA,
        timeout=30.0,
    )
    async def ask_home(self, *, text: str = "", language: str = "", **_) -> dict[str, Any]:
        """兜底：把原话交给 HA 的 Assist。"""
        if self.client is None:
            return _tool_error("not_configured", _NOT_CONFIGURED_HINT)
        sentence = str(text or "").strip()
        if not sentence:
            return _tool_error("missing_parameter", "text 不能为空")

        try:
            payload = await self.client.ask(sentence, language=str(language or ""))
        except HaAuthError as exc:
            return _tool_error("token_expired", str(exc))
        except HaConnectionError as exc:
            return _tool_error("ha_unreachable", str(exc))
        except HaServiceError as exc:
            return _tool_error("assist_failed", str(exc))

        response = payload.get("response") if isinstance(payload, dict) else None
        response = response if isinstance(response, dict) else {}
        response_type = str(response.get("response_type") or "unknown")
        speech = ""
        speech_block = response.get("speech")
        if isinstance(speech_block, dict):
            plain = speech_block.get("plain")
            if isinstance(plain, dict):
                speech = str(plain.get("speech") or "")

        if response_type == "error":
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            return _tool_error(
                "assist_no_match",
                speech or "Home Assistant 没有理解这句话",
                assist_code=str(data.get("code") or "unknown"),
            )

        return _tool_payload(
            {
                "ok": True,
                "response_type": response_type,
                "speech": speech,
                "conversation_id": str(payload.get("conversation_id") or ""),
                "continue_conversation": bool(payload.get("continue_conversation")),
            }
        )

    # ========== 内部：控制流水线 ==========
    @staticmethod
    def _as_targets(value: Any) -> list[str]:
        """规范化 targets：接受 list 或单个字符串。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _plan(
        self,
        entity: dict[str, Any],
        target: str,
        action: str,
        params: dict[str, Any],
        results: list[dict[str, Any]],
        planned: list[tuple[dict[str, Any], ServiceCall]],
    ) -> None:
        """把一台设备编入执行计划，或就地写入一条失败结果。"""
        entity_id = str(entity.get("entity_id", ""))
        base = {
            "target": target,
            "entity_id": entity_id,
            "name": entity.get("name", ""),
            "action": action,
            "params": params,
        }

        allowed, reason = self._whitelist.decision(entity)
        if not allowed:
            results.append({**base, "status": "blocked", "reason": reason, "risk": entity.get("risk", "")})
            self._audit.record(action=action, targets=[entity_id], result=f"blocked:{reason}")
            return

        call, unsupported = build_call(entity, action, params)
        if call is None:
            results.append(
                {
                    **base,
                    "status": "unsupported",
                    "message": f"{entity.get('domain', '')} 不支持动作 {action}",
                    "capabilities": _capabilities(entity),
                }
            )
            return
        if unsupported:
            results.append(
                {
                    **base,
                    "status": "unsupported_params",
                    "message": "该设备不支持这些参数，动作未执行",
                    "unsupported_params": unsupported,
                    "capabilities": _capabilities(entity),
                }
            )
            return

        planned.append((entity, call))

    async def _readback(self, results: list[dict[str, Any]], action: str) -> None:
        """执行后回读状态，判定"真的变了没有"。

        这是草案里最容易漏、但最有用的一条：HA 的服务调用只要实体在线就会返回
        成功，哪怕物理设备卡住/离线。不回读的话插件会替设备撒谎。
        """
        if self.client is None:
            return
        ok_items = [item for item in results if item.get("status") == "ok" and item.get("entity_id")]
        if not ok_items or len(ok_items) > _READBACK_LIMIT:
            for item in ok_items:
                item["verified"] = None
            return

        for item in ok_items:
            entity_id = str(item["entity_id"])
            try:
                state_obj = await self.client.get_entity_state(entity_id)
            except Exception:
                item["verified"] = None
                continue
            if not state_obj:
                item["verified"] = None
                continue

            state_after = str(state_obj.get("state") or "")
            attributes = state_obj.get("attributes")
            item["state_after"] = state_after
            self._sync_snapshot_state(entity_id, state_after, attributes if isinstance(attributes, dict) else None)

            expected = _expected_state(action, str(item.get("service", "")).split(".", 1)[0])
            if expected is None:
                item["verified"] = None
            else:
                item["verified"] = state_after in expected

    def _describe(self, entity: dict[str, Any], facts: dict[str, Any]) -> str:
        """把状态事实拼成一句人话。

        只用于 `@plugin_entry` 的 message 字段（面板/analyzer 展示）；对话路径
        给的是结构化的 `facts`，由 LLM 自己组织语言，不经过这里——所以这里的
        译文只影响"给人看的字符串"，不会污染模型理解。
        """
        name = entity.get("name") or " ".join(
            part for part in (entity.get("area_name", ""), entity.get("name", "")) if part
        )
        domain = entity.get("domain", "")
        state = str(entity.get("state", ""))

        if not entity.get("is_online", True):
            return self._t("state.unavailable", "「{name}」当前离线", name=name)

        if domain in ("light", "switch", "input_boolean"):
            if not facts.get("power"):
                return self._t("state.off", "「{name}」已关闭", name=name)
            if "brightness" in facts:
                return self._t(
                    "state.on_with_brightness",
                    "「{name}」已开启，亮度 {brightness}%",
                    name=name,
                    brightness=facts["brightness"],
                )
            return self._t("state.on", "「{name}」已开启", name=name)

        if domain == "fan":
            if not facts.get("power"):
                return self._t("state.off", "「{name}」已关闭", name=name)
            if "percentage" in facts:
                return self._t("state.fan", "「{name}」风速 {percentage}%", name=name, percentage=facts["percentage"])
            return self._t("state.on", "「{name}」已开启", name=name)

        if domain == "climate":
            parts: list[str] = []
            if facts.get("current_temperature") is not None:
                parts.append(
                    self._t("state.current_temp", "当前 {temp}°C", temp=facts["current_temperature"])
                )
            if facts.get("target_temperature") is not None:
                parts.append(self._t("state.target_temp", "目标 {temp}°C", temp=facts["target_temperature"]))
            if facts.get("fan_mode"):
                parts.append(self._t("state.fan_mode", "风速 {mode}", mode=facts["fan_mode"]))
            detail = ", ".join(parts) if parts else self._t("state.hvac_mode", "模式 {mode}", mode=state)
            return self._t("state.climate", "「{name}」{detail}", name=name, detail=detail)

        if domain == "cover":
            position = facts.get("position")
            if position is not None:
                return self._t(
                    "state.cover_position",
                    "「{name}」当前开合度 {position}%",
                    name=name,
                    position=position,
                )
            label = self._t("state.cover_open", "开启") if facts.get("open") else self._t("state.cover_closed", "关闭")
            return self._t("state.cover", "「{name}」{state}", name=name, state=label)

        if domain == "valve":
            label = self._t("state.valve_open", "开启") if facts.get("open") else self._t("state.valve_closed", "关闭")
            return self._t("state.valve", "「{name}」阀门{state}", name=name, state=label)

        if domain == "water_heater":
            return self._t(
                "state.water_heater",
                "「{name}」{mode}，目标 {temp}°C",
                name=name,
                mode=state,
                temp=facts.get("target_temperature", "-"),
            )

        if domain == "media_player":
            parts = []
            if facts.get("volume") is not None:
                parts.append(self._t("state.volume", "音量 {volume}%", volume=facts["volume"]))
            if facts.get("media_title"):
                parts.append(self._t("state.media_title", "正在播放 {title}", title=facts["media_title"]))
            detail = ", ".join(parts) if parts else state
            return self._t("state.media", "「{name}」{detail}", name=name, detail=detail)

        if domain == "lock":
            key, default = (
                ("state.locked", "「{name}」已上锁") if facts.get("locked") else ("state.unlocked", "「{name}」未上锁")
            )
            return self._t(key, default, name=name)

        if domain == "alarm_control_panel":
            if facts.get("armed"):
                return self._t(
                    "state.alarm_armed",
                    "「{name}」已布防（{mode}）",
                    name=name,
                    mode=facts.get("arm_mode") or state,
                )
            return self._t("state.alarm_disarmed", "「{name}」已撤防", name=name)

        if domain in ("sensor", "binary_sensor"):
            return self._t(
                "state.sensor",
                "「{name}」当前为 {value}{unit}",
                name=name,
                value=facts.get("value", state),
                unit=facts.get("unit", ""),
            )

        return self._t("state.generic", "「{name}」当前状态：{state}", name=name, state=state)
