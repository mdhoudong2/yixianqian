"""lib/bitable_client.py 字段值解析纯函数单测（不联网）。"""
from lib import bitable_client as bc


def test_get_field_text_plain_list():
    assert bc.get_field_text({"姓名": [{"text": "张三"}]}, "姓名") == "张三"


def test_get_field_text_formula_wrapper():
    fields = {"姓名": {"type": 1, "value": [{"text": "李四"}]}}
    assert bc.get_field_text(fields, "姓名") == "李四"


def test_get_field_text_multi_segments_joined():
    fields = {"内容": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
    assert bc.get_field_text(fields, "内容") == "abc"


def test_get_field_text_missing_default():
    assert bc.get_field_text({}, "不存在", "默认") == "默认"


def test_get_field_number_int():
    assert bc.get_field_number({"爱心": 3}, "爱心") == 3
    assert isinstance(bc.get_field_number({"爱心": 3}, "爱心"), int)


def test_get_field_number_formula_and_invalid():
    assert bc.get_field_number({"v": {"type": 2, "value": [{"value": 5}]}}, "v") == 5
    assert bc.get_field_number({"v": [{"value": 2.5}]}, "v") == 2.5
    assert bc.get_field_number({"v": "abc"}, "v", -1) == -1


def test_get_select_value_forms():
    assert bc.get_select_value({"性别": [{"name": "男"}]}, "性别") == "男"
    assert bc.get_select_value({"性别": {"type": 3, "value": [{"name": "女"}]}}, "性别") == "女"
    assert bc.get_select_value({}, "性别", "未填") == "未填"


def test_get_multi_select_value():
    fields = {"爱好": [{"name": "篮球"}, {"name": "音乐"}]}
    assert bc.get_multi_select_value(fields, "爱好") == ["篮球", "音乐"]
    assert bc.get_multi_select_value({"爱好": "读书"}, "爱好") == ["读书"]
    assert bc.get_multi_select_value({}, "爱好") == []


def test_get_attachment_tokens():
    fields = {"照片": [{"file_token": "tok1"}, {"file_token": "tok2"}, {"x": 1}]}
    assert bc.get_attachment_tokens(fields, "照片") == ["tok1", "tok2"]
    assert bc.get_attachment_tokens({}, "照片") == []


def test_get_datetime_value_ms():
    assert bc.get_datetime_value({"生日": 946684800000}, "生日") == "2000-01-01"


def test_get_datetime_value_negative_ms_pre1970():
    # 1970 年前的负毫秒时间戳：验证毫秒→秒换算分支（期望值按本地时区换算）
    import time
    expected = time.strftime("%Y-%m-%d", time.localtime(-1210752000))
    assert bc.get_datetime_value({"生日": -1210752000000}, "生日") == expected


def test_get_datetime_value_dict_and_invalid():
    assert bc.get_datetime_value({"t": {"value": 946684800}}, "t") == "2000-01-01"
    assert bc.get_datetime_value({"t": "垃圾"}, "t", "缺") == "垃圾"


def test_get_phone_value_forms():
    assert bc.get_phone_value({"电话": [{"number": "13800000000"}]}, "电话") == "13800000000"
    assert bc.get_phone_value({"电话": "13900000000"}, "电话") == "13900000000"
    assert bc.get_phone_value({"电话": {"number": "13700000000"}}, "电话") == "13700000000"
    assert bc.get_phone_value({}, "电话", "无") == "无"
