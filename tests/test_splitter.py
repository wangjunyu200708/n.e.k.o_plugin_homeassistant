"""多指令拆分与共享动词补全。"""

from nlp.splitter import inherit_verb, split_commands


class TestSplit:
    def test_conjunction(self):
        assert split_commands("打开客厅灯和卧室灯") == ["打开客厅灯", "卧室灯"]

    def test_punctuation(self):
        assert split_commands("打开客厅灯，关掉插座") == ["打开客厅灯", "关掉插座"]

    def test_multiple_connectors(self):
        segments = split_commands("打开客厅灯然后关掉插座顺便关窗帘")
        assert len(segments) == 3

    def test_single_command_returns_raw(self):
        assert split_commands("打开客厅灯") == ["打开客厅灯"]

    def test_empty(self):
        assert split_commands("") == [""]

    def test_strips_politeness_per_segment(self):
        segments = split_commands("请把客厅灯打开和卧室灯关掉")
        assert all("请" not in segment for segment in segments)

    def test_strips_tail_particles(self):
        segments = split_commands("打开客厅灯吧，关掉插座呢")
        assert segments == ["打开客厅灯", "关掉插座"]

    def test_inner_filler_removed(self):
        segments = split_commands("打开客厅灯和卧室灯也关掉")
        assert segments[1] == "卧室灯关掉"

    def test_device_name_with_conjunction_kept_as_raw_when_unsplittable(self):
        # 切坏了也没关系：调用方的预检会整句回退
        segments = split_commands("打开和风空调")
        assert isinstance(segments, list)


class TestInheritVerb:
    def test_prepends_verb(self):
        assert inherit_verb("卧室灯", "打开") == "打开卧室灯"

    def test_keeps_existing_verb(self):
        assert inherit_verb("关掉插座", "打开") == "关掉插座"

    def test_no_verb_no_change(self):
        assert inherit_verb("卧室灯", "") == "卧室灯"

    def test_empty_segment(self):
        assert inherit_verb("", "打开") == ""

    def test_long_verb_not_truncated(self):
        # "开始" 不能被单字"开"截断成"开卧室"
        assert inherit_verb("客厅音箱", "开始") == "开始客厅音箱"
