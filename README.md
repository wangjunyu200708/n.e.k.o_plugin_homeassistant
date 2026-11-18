# Home Assistant 智能家居 · N.E.K.O 插件

用一句话控制你**已有的** Home Assistant 实例里的灯光、空调、窗帘、影音、门锁与场景。

> 本插件**不内置 Home Assistant**，也不负责安装它。它是一个客户端：你需要在局域网（或可访问的网络）里已经跑着一个 Home Assistant，并给它签发一个长期访问令牌。

---

## 它解决什么问题

Home Assistant 有 3000+ 集成，能把米家、涂鸦、HomeKit、Zigbee、ESPHome、Modbus 等各家设备收进同一个实体模型。但它的原生交互是面板点按 + 自动化编排，用嘴说话并不顺手。

本插件补的就是这一层：让 N.E.K.O 的对话模型直接操作 HA 里的实体。

```
你说的话  ──▶  N.E.K.O 的 LLM（function calling）
                    │  find_devices / get_state / control_devices / run_scene / ask_home
                    ▼
              工具层（__init__.py）──▶ ha_api（caps 能力归一化 + REST/WS）
                    │
                    ▼
              Home Assistant REST / WebSocket API
```

**插件里没有第二套自然语言解析。** 理解「客厅灯调暗点」交给 NEKO 自己的模型——它本来就擅长把一句话拆成 `targets` + `action` + `params`。插件只做三件事：把设备索引暴露清楚、把动作翻译成 HA 服务、把危险动作拦住。

这样做的代价是模糊表达依赖模型质量；收益是插件里不再有一层要自己养的中文分词、同义词表与消歧规则——那层东西每加一个 domain 就要改三处，而且没法回归验证。

---

## 前置条件

1. 一个**在运行的** Home Assistant（2023.x 及以上）
2. 一个长期访问令牌：HA 面板 → 左下角头像 → **安全** → 长期访问令牌 → 创建令牌
3. N.E.K.O 所在机器能访问到 HA 的地址（通常是 `http://homeassistant.local:8123` 或 `http://192.168.x.x:8123`）

---

## 支持的 domain

| domain | 中文 | 能力 |
| --- | --- | --- |
| `light` | 灯 | 开关、亮度（0-255 → 0-100）、色温、颜色、灯效 |
| `switch` | 开关/插座 | 开关 |
| `fan` | 风扇 | 开关、风速百分比、预设模式 |
| `climate` | 空调/地暖 | 开关、目标温度、模式、风速、摆风、预设 |
| `cover` | 窗帘/车库门/大门 | 开合、位置百分比、停止 |
| `valve` | 阀门（水/燃气） | 开合、位置百分比 |
| `water_heater` | 热水器 | 开关、目标温度、工作模式 |
| `media_player` | 音箱/电视 | 播放暂停、上下曲、音量、音源、静音 |
| `lock` | 门锁 | 上锁 / 解锁 / 开门闩 |
| `alarm_control_panel` | 安防面板 | 在家/离家/夜间布防、撤防 |
| `scene` | 场景 | 触发 |
| `script` | 脚本 | 运行 / 停止 |
| `input_boolean` | 虚拟开关 | 开关 |

其余 domain（`vacuum` / `humidifier` / `camera` / `sensor` 等）不提供控制。传感器仍可**查询**——问「客厅现在几度」是能答的。

---

## 安全模型

### 一、白名单按风险三档自动归类

| 档位 | 包含 | 默认 |
| --- | --- | --- |
| 低 | 灯、风扇、影音、场景、脚本、虚拟开关、普通窗帘 | **允许** |
| 中 | 开关/插座、空调 | **允许**（面板会标出来） |
| 高 | 门锁、报警面板、阀门（水/燃气）、热水器、车库门/大门/栅栏门类 cover | **阻止** |

高危设备**只能逐个授权**，不提供「整类一键全开」——面板上点「全选开关」能生效，点「全选门锁」只会改到那些本来就不高危的。

授权只决定「能不能控」，不取消二次确认：授权过的门锁在解锁时**仍然**要过确认。

### 二、危险方向两阶段确认

只拦「让物理世界暴露」的方向：解锁、开车库门、开阀门、撤防、给热水器加热。**关、上锁、布防一律放行**——它们让世界更安全，每次都弹确认只会训练用户无脑确认。

```
> 把大门打开
< 这是高危动作，尚未执行。即将对「大门门锁」执行「解锁」。
  请回复确认码：a3f9c1d2（120 秒内有效）

> a3f9c1d2
< 已执行：lock.unlock
```

令牌是一次性的、绑定了（实体集合 + 动作 + 参数）指纹、120 秒过期、只保留最新一条。换目标或换参数即失效。

> ⚠️ **威胁模型边界：防误判，不防恶意。** 令牌经工具返回值交给 LLM，再由 LLM 转述给用户；宿主会把工具输出写进对话历史，所以模型天然知道令牌，理论上可以不等用户回复就自己重放。彻底堵住这个口子需要让令牌绕开 LLM 走人的通道（例如面板上点确认），那是另一套交互，不在本版范围内。

### 三、令牌不落到明文

| 平台 | 存储 |
| --- | --- |
| Windows | DPAPI（`CryptProtectData`），绑定当前用户 + 当前机器，零依赖 |
| macOS / Linux | 系统钥匙串（需装 `keyring`） |
| 都不可用 | **只存进程内存**，面板明确提示「重启需重新填写」 |

**任何情况下都不会静默降级成明文文件。** 若检测到早期构建留下的明文 `credential.json`，会转存为加密文件并删除明文。

### 四、执行后回读状态

HA 的服务调用只要实体在线就返回成功——哪怕物理设备卡住或离线。所以每次控制后插件会回读状态并给出 `verified`：

* `true` —— 状态确实变成了期望值
* `false` —— 命令发出去了但设备没反应（通常是离线或物理卡住）
* `null` —— 该动作无法用状态判定（例如影音的 `turn_on`）

### 五、审计日志

`plugins/homeassistant/data/audit.jsonl` 记录动作、目标、结果、风险档位，**绝不记录令牌**（JWT 与长十六进制串会被兜底替换成 `<redacted-*>`）。超过 1 MB 停止写入。

---

## 工具面

| 工具 | 类型 | 说明 |
| --- | --- | --- |
| `find_devices` | `@llm_tool` | 按区域 / 类型 / 名字查设备，返回 entity_id、风险档位、是否可控、能力列表 |
| `get_state` | `@llm_tool` | 批量查状态，返回结构化 `facts` |
| `control_devices` | `@llm_tool` | 批量控制，**逐台返回** `ok / not_found / ambiguous / blocked / unsupported / unsupported_params / failed` |
| `run_scene` | `@llm_tool` | 执行场景或脚本 |
| `ask_home` | `@llm_tool` | 兜底：把原话转给 HA 内置 Assist。不保证中文 intent 覆盖度 |
| `query_entity_state` / `list_entities` / `list_areas` / `get_whitelist` / `set_entity_allowed` / `set_entity_alias` / `get_entity_aliases` / `get_cached_entities` | `@plugin_entry` | 查询与配置，面板与 analyzer 用 |
| `test_connection` / `save_connection` / `clear_connection` / `get_status` / `reload_entities` / `open_ui` | `@plugin_entry` | 连接管理 |

控制类走 `@llm_tool`，查询与配置类走 `@plugin_entry`。宿主会把 analyzer 视野里的 `__llm_tool__*` 剥掉，所以两者不能混挂。

### 目标解析

`targets` 接受 `entity_id`、设备名、别名、设备名或**区域名**。匹配优先级：

```
entity_id 精确 → 名字/别名精确 → 区域+名字 精确 → 区域名 → 子串包含
```

任何一级命中多个都返回 `ambiguous` 并附候选，**插件不替用户挑一个**。

刻意**不**把区域名展开成「该区域全部设备」：客厅里可能有灯、音箱和窗帘，一句「开客厅」若展开成 `turn_on`，会把窗帘一起打开（`cover` 的 `turn_on` 就是 `open_cover`）。要批量控制就让模型先 `find_devices(area=..., domain=...)` 拿到明确清单再下发——多一次往返，换掉一次不可逆的误动作。

---

## 降级行为

区域名来自 WebSocket 的三张注册表（area / device / entity）。任一环节不可用时插件**不报错**，而是降级并在 `get_status` / 工具返回的 `degraded` 字段里说明：

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

# 发版前严格检查
./.venv/Scripts/python.exe -m plugin.neko_plugin_cli.cli check E:/n.e.k.o_plugin_homeassistant -r
```

> CI 的 ruff 钉死 0.12.4 且带 `--no-respect-gitignore`。系统 ruff（0.16.x）在仓库内直接跑会得出相反结论，**以 `neko-plugin lint` 为准**。

目录结构：

```
__init__.py      插件主类：5 个 llm 工具、配置入口、执行与回读、确认流程
ha_api/          caps 能力归一化 + 三级风险分级、REST/WS 客户端、service 构造
resolver.py      目标解析（确定性匹配，绝不猜）
security.py      白名单、确认令牌、审计日志
secret_store.py  DPAPI / 钥匙串 / 内存 三级令牌存储
i18n/            8 语言 × 103 key
static/          托管面板 index.html + 共用 i18n.js
docs/            quickstart 引导（8 语言）
tests/           259 项
```

## 许可

MIT
