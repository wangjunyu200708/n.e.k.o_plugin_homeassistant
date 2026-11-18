# Home Assistant 智能家居 · N.E.K.O 插件

用自然语言控制你**已有的** Home Assistant 实例里的灯光、空调、窗帘、影音与场景。

> 本插件**不内置 Home Assistant**，也不负责安装它。它是一个客户端：你需要在局域网（或可访问的网络）里已经跑着一个 Home Assistant，并给它签发一个长期访问令牌。

---

## 它解决什么问题

Home Assistant 有 3000+ 集成，能把米家、涂鸦、HomeKit、Zigbee、ESPHome、Modbus 等各家设备收进同一个实体模型。但它的原生交互是面板点按 + 自动化编排，用嘴说话并不顺手。

本插件补的就是这一层：把「打开客厅灯」「空调调到 26 度」「音量调到 40」这类口语，翻译成 HA 的 service 调用。

```
你说的话  ──▶  nlp 层（分词/归一化/动词/数值/设备匹配/能力校验）
                    │
                    ▼  CommandIR（与品牌无关的中间表示）
              ha_api 层（caps 能力归一化 → domain.service 调用）
                    │
                    ▼
              Home Assistant REST / WebSocket API
```

三层折算让 nlp 层**永远不知道**设备的品牌和 domain —— 加一个新 domain 只需要改 `ha_api/`，不动自然语言部分。

---

## 前置条件

1. 一个**在运行的** Home Assistant（2023.x 及以上）
2. 一个长期访问令牌：HA 面板 → 左下角头像 → **安全** → 长期访问令牌 → 创建令牌
3. N.E.K.O 所在机器能访问到 HA 的地址（通常是 `http://homeassistant.local:8123` 或 `http://192.168.x.x:8123`）

> 令牌只在保存时写入一次，落盘在 `plugins/homeassistant/data/credential.json`（权限收紧、原子写入）。插件对外展示时只回显尾 4 位。

---

## 支持的 domain

| domain | 中文 | 能力 |
| --- | --- | --- |
| `light` | 灯 | 开关、亮度（0-255 → 0-100）、色温、颜色 |
| `switch` | 开关/插座 | 开关 |
| `climate` | 空调/地暖 | 开关、目标温度、模式、风速 |
| `cover` | 窗帘/车库门 | 开合、位置百分比 |
| `media_player` | 音箱/电视 | 播放暂停、音量、音源、静音 |
| `scene` | 场景 | 触发 |

其余 domain（`fan` / `vacuum` / `lock` / `sensor` 等）会被识别但明确拒绝或标记为只读，不会静默失败。

---

## 危险动作的两步确认

`cover` 里 `device_class` 为 `garage` / `garage_door` / `gate` / `door` 的实体，以及 `lock` 的**解锁**，属于危险动作。插件不会直接执行，而是返回一个 8 位确认码：

```
> 打开车库门
< 即将执行「打开车库门」，此操作有安全风险。确认码：a3f9c1d2
  请在 120 秒内回复该确认码以继续。

> a3f9c1d2
< 已执行：打开车库门
```

令牌只存在进程内存，一次性消费，只保留最新一条。宿主重启即失效。

---

## 工具面

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `smart_control` | `@llm_tool` | 自然语言统一控制，参数 `{"command": "打开客厅灯"}`。**只能由对话中的 LLM 调用**，不能用 `/runs` + entry_id 触发 |
| `query_entity_state` | `@plugin_entry` | 按名字查询实体状态 |
| `list_entities` | `@plugin_entry` | 列出实体，支持 `domain` / `area` 过滤 |
| `list_areas` | `@plugin_entry` | 列出区域（依赖 WebSocket 注册表） |
| `set_entity_alias` | `@plugin_entry` | 给实体起别名，改善匹配 |
| `test_connection` / `save_connection` / `clear_connection` / `get_status` | `@plugin_entry` | 连接管理 |

控制类走 `@llm_tool`，查询与配置类走 `@plugin_entry`。宿主会把 analyzer 视野里的 `__llm_tool__*` 剥掉，所以两者不能混挂。

---

## 降级行为

区域名来自 WebSocket 的四张注册表（area / device / entity / entity-for-display）。任一环节不可用时插件**不报错**，而是降级并在 `get_status` 里说明：

| 情形 | 标志 | 表现 |
| --- | --- | --- |
| 宿主未装 `websockets` | `DEGRADED_NO_WS_LIB` | 无区域名，按实体 `friendly_name` 子串匹配 |
| WS 握手失败 | `DEGRADED_WS_FAILED` | 同上 |
| 令牌非管理员 | `DEGRADED_NOT_ADMIN` | 用 `list_for_display` 缩写字段（`ei/ai/di/en/hb`） |

实体快照三级兜底：内存（60s TTL）→ 网络 → `entities_cache.json`（校验 `schema_version` 与 `base_url`，避免换了实例后读到旧设备）。

---

## 开发

```bash
# 依赖（由宿主主环境提供，无需额外安装）
# httpx / websockets / pydantic

# 测试
python -m pytest tests/ -q

# 权威 lint —— 必须用宿主自带的 CLI，不要用系统 ruff
cd /e/NEKOdm/N.E.K.O
./.venv/Scripts/python.exe -m plugin.neko_plugin_cli.cli lint E:/n.e.k.o_plugin_homeassistant
```

> CI 的 ruff 钉死 0.12.4 且带 `--no-respect-gitignore`。系统 ruff（0.16.x）在仓库内直接跑会得出相反结论，**以 `neko-plugin lint` 为准**。

目录结构：

```
__init__.py      插件主类：入口、执行、两步确认、i18n 兜底
ha_api/          caps 能力归一化、REST/WS 客户端、service 构造
nlp/             分词、归一化、动词、数值、设备匹配、能力校验、路由
i18n/            8 语言
static/          托管面板 index.html
docs/            quickstart 引导（8 语言）
tests/           280 项
```

## 许可

MIT
