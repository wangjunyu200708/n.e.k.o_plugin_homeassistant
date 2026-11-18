"""意图词表：开关 / 查询 / 属性 / 模式 / 确认词等常量。

**这是与 mijia 差异最大的一层**。米家的词表是"设备型号口吻"（主灯、暖光、
扫拖、集尘），HA 的词表是"domain 口吻"（light / climate / cover /
media_player / scene）。所以这里整体重写，只保留"分界动词""调整修饰词"
"极值词""颜色表"这些与设备体系无关的部分。

一个刻意的取舍：**HA 的枚举值无法穷举**。灯效可以是任意字符串，音源可以是
"HDMI 1"/"Spotify"/自定义名字。所以 `MODE_TERMS` 只收常见词用于*分界*，
真正的取值合法性由执行层的 `match_option()` 对着实体的 caps 校验。
"""

import re
from typing import Optional

# ── 场景执行动词 ──
SCENE_VERBS = r"执行|运行|触发|激活"
SCENE_RE = re.compile(r"(?:执行|运行|触发|激活)\s*(.+)")
# 打开/开启/启动 X模式/场景 → 场景（"打开回家模式"不是开关"回家模式"这个设备）
SCENE_MODE_RE = re.compile(r"(?:打开|开启|启动|运行|激活)\s*(.+?)(?:模式|场景)\s*$")

# 裸口语 → 场景关键词候选（按优先级）。返回多个是因为各家 HA 场景命名不同：
# 有人叫"晚安"，有人叫"睡眠模式"，有人叫"睡觉"。交给实体匹配逐个试。
SCENE_PHRASE_MAP: dict[str, tuple[str, ...]] = {
    "我回家了": ("回家", "到家"),
    "我到家了": ("到家", "回家"),
    "我回来了": ("回家", "到家"),
    "到家了": ("到家", "回家"),
    "下班了": ("回家", "下班"),
    "晚安": ("晚安", "睡眠", "睡觉"),
    "睡觉了": ("睡觉", "睡眠", "晚安"),
    "我要睡了": ("睡觉", "睡眠", "晚安"),
    "睡了": ("睡觉", "睡眠"),
    "出门了": ("离家", "出门", "外出"),
    "我出门了": ("离家", "出门", "外出"),
    "看电影": ("影院", "电影", "观影"),
}


def detect_scene(command: str) -> list[str]:
    """识别场景意图，返回场景关键词候选列表；非场景返回空列表。

    覆盖三种形态：
    1. 执行/运行/触发 X → [X]
    2. 打开/开启/启动 X模式/场景 → [X]
    3. 裸口语别名（我回家了/晚安等）→ 多个候选关键词
    """
    cmd = (command or "").strip()
    m = SCENE_RE.match(cmd)
    if m:
        # "执行回家场景" 的捕获是 "回家场景"，场景实体名里没有"场景"二字
        return [_strip_scene_suffix(m.group(1))]
    m = SCENE_MODE_RE.match(cmd)
    if m:
        return [_strip_scene_suffix(m.group(1))]
    return list(SCENE_PHRASE_MAP.get(cmd, ()))


def _strip_scene_suffix(name: str) -> str:
    return re.sub(r"(?:场景|模式)$", "", (name or "").strip()).strip()


# ── 开关指令（前缀动词）──
SWITCH_CMD_RE = re.compile(r"(?:打开|开启|关闭|关掉|开|关)\s*\S")
# 动作动词开头的命令不算开关指令（"开始播放"不该被拆成"开"+"始播放"）
ACTION_VERB_PREFIX_RE = re.compile(r"^(?:开始|启动|继续|暂停|停止|播放|静音|取消静音|下一首|上一个|下一个)\s*\S")

# ── 查询意图词 ──
QUERY_RE = re.compile(
    r"(?:是多少|多少度|几度|怎么样|什么状态|状态如何|开着吗|关了吗|开着没|开了吗|"
    r"多亮|多暗|多热|多冷|多少|查询|看看|看一下|还剩|还有多久)"
)
# 查询时从设备名尾部剥离的属性后缀
QUERY_PROP_SUFFIX_RE = re.compile(r"(?:温度|湿度|亮度|色温|音量|风速|开合度|位置|电量|状态|单位)$")
# 尾部询问/语气后缀（开关/控制分支剥离，防止污染设备名）
QUERY_SUFFIX_RE = re.compile(r"(?:怎么样|怎样|如何|呢|吗)$")
# 查询关键词 → 隐含属性
QUERY_KEYWORD_PROP_MAP = {
    "多少度": "温度",
    "几度": "温度",
    "多亮": "亮度",
    "多暗": "亮度",
    "多热": "温度",
    "多冷": "温度",
}

# ── 属性词（分界用，长词在前）──
PROP_TERMS = r"开合度|输入源|色温|亮度|温度|音量|风速|湿度|位置|开度|角度|档位|预设|音源|效果|颜色|色彩"

# ── 模式词（仅用于分界与常见词识别，不当作合法取值全集）──
MODE_TERMS = (
    r"制冷|制热|除湿|送风|自动|冷暖|待机|"
    r"睡眠|节能|静音|强力|舒适|标准|自动风|低风|中风|高风|"
    r"阅读|影院|观影|夜灯|氛围|明亮|柔和|自然|暖光|冷光|"
    r"电视|电脑|娱乐|游戏|音乐|电影|新闻|运动|"
    r"蓝牙|光纤|同轴|网络|电视直播"
)

# ── 属性/模式设置动词（分界用，长词在前）──
SPLIT_VERBS = r"设置为|调到|调成|调为|设为|调至|切换到|切换至|切到|设定|设成|设置|调整|开到|开至"

# ── 相对调整修饰词 ──
ADJUST_UP = r"高|大|亮|暖|多|强|快|升|加"
ADJUST_DOWN = r"低|小|暗|冷|少|弱|慢|降|减"

# ── 极值修饰词 ──
EXTREME_TERMS = r"最高|最低|最亮|最暗|最大|最小|最强|最弱|最快|最慢|最暖|最冷|最多|最少"
EXTREME_MAX = {"最高", "最亮", "最大", "最强", "最快", "最暖", "最多"}

# ── 颜色词 → RGB ──
COLOR_MAP = {
    "红": 0xFF0000,
    "红色": 0xFF0000,
    "绿": 0x00FF00,
    "绿色": 0x00FF00,
    "蓝": 0x0000FF,
    "蓝色": 0x0000FF,
    "黄": 0xFFFF00,
    "黄色": 0xFFFF00,
    "紫": 0xFF00FF,
    "紫色": 0xFF00FF,
    "橙": 0xFFA500,
    "橙色": 0xFFA500,
    "粉": 0xFFC0CB,
    "粉色": 0xFFC0CB,
    "粉红": 0xFFC0CB,
    "白": 0xFFFFFF,
    "白色": 0xFFFFFF,
    "青": 0x00FFFF,
    "青色": 0x00FFFF,
    "黑": 0x000000,
    "黑色": 0x000000,
    "暖白": 0xFFF4E0,
    "冷白": 0xF0F8FF,
}

# ── HVAC 模式：中文 → HA 的 hvac_mode 取值 ──
HVAC_MODE_MAP = {
    "制冷": "cool",
    "冷风": "cool",
    "冷": "cool",
    "制热": "heat",
    "暖风": "heat",
    "热": "heat",
    "自动": "auto",
    "冷暖": "heat_cool",
    "自动冷热": "heat_cool",
    "送风": "fan_only",
    "风扇": "fan_only",
    "吹风": "fan_only",
    "除湿": "dry",
    "干燥": "dry",
    "关": "off",
    "关闭": "off",
    "待机": "off",
}

# ── 两步确认词 ──
# 只做**整句**匹配：'好的，把灯打开' 不是纯确认，不能被当成对上一句的答复。
CONFIRM_WORDS = frozenset(
    {
        "确认",
        "确定",
        "是",
        "是的",
        "对",
        "好",
        "好的",
        "可以",
        "执行",
        "继续",
        "嗯",
        "没错",
        "yes",
        "ok",
        "y",
    }
)
DENY_WORDS = frozenset({"取消", "不用", "不要", "别", "算了", "停止", "no", "cancel", "n"})

# ── 危险动作的用户话术片段（用于确认提示里说明"这条到底干了什么"）──
DANGER_LABELS = {
    "unlock": "解锁",
    "open_cover": "开门",
    "alarm_disarm": "关闭警报",
}


def is_confirmation(text: str) -> bool:
    """整句是否为纯确认/否定。"""
    return (text or "").strip().lower() in CONFIRM_WORDS


def is_denial(text: str) -> bool:
    return (text or "").strip().lower() in DENY_WORDS


def normalize_mode_word(word: str) -> Optional[str]:
    """中文模式词 → HA 取值；非已知模式词返回 None（交给执行层模糊匹配）。"""
    return HVAC_MODE_MAP.get((word or "").strip())
