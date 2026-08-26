"""pass_card_filters 年龄区间（age_min/age_max）单测（不联网）。

覆盖：边界值（周岁恰好等于上/下限）、生日已过/未过、无生日用户、
毫秒/秒时间戳、1970 前负时间戳、未来生日、闰日生日、字符串与异常参数防御。
"""
import calendar
import datetime
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

from app import pass_card_filters


def _shifted_birthday(years_ago, day_offset=0):
    """构造生日日期字符串：今天往前 years_ago 年，再偏移 day_offset 天。

    day_offset=0 表示生日正是今天（周岁恰好 = years_ago）；
    day_offset=-1 生日昨天（已满 years_ago 周岁）；+1 生日明天（还差一天满周岁）。
    2 月 29 日遇平年回退到 2 月 28 日，保证日期合法。
    """
    today = datetime.date.today()
    try:
        base = today.replace(year=today.year - years_ago)
    except ValueError:
        base = datetime.date(today.year - years_ago, 2, 28)
    return (base + datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d")


def _fields(birthday_str):
    """按 'YYYY-MM-DD' 构造用户表字段（生日为毫秒时间戳，UTC 零点）"""
    dt = datetime.datetime.strptime(birthday_str, "%Y-%m-%d")
    return {"生日": calendar.timegm(dt.timetuple()) * 1000}


def _age_of(birthday_str):
    """与实现一致的周岁算法（today 为本地日期）"""
    today = datetime.date.today()
    by, bm, bd = (int(x) for x in birthday_str.split("-"))
    return today.year - by - ((today.month, today.day) < (bm, bd))


def test_no_age_filter_passes_all():
    assert pass_card_filters({}, {}) is True
    assert pass_card_filters({"生日": 946684800000}, {}) is True
    assert pass_card_filters({}, {"age_min": None, "age_max": None}) is True


def test_age_max_excludes_older_and_keeps_boundary():
    old = _fields(_shifted_birthday(35, day_offset=-1))   # 已满 35 岁
    boundary = _fields(_shifted_birthday(30, day_offset=0))  # 恰好 30 岁
    young = _fields(_shifted_birthday(25, day_offset=+1))  # 25 岁还差一天
    assert pass_card_filters(old, {"age_max": 30}) is False
    assert pass_card_filters(boundary, {"age_max": 30}) is True
    assert pass_card_filters(young, {"age_max": 30}) is True


def test_age_min_excludes_younger_and_keeps_boundary():
    young = _fields(_shifted_birthday(20, day_offset=+1))   # 19 岁（差一天）
    boundary = _fields(_shifted_birthday(20, day_offset=0))  # 恰好 20 岁
    old = _fields(_shifted_birthday(21, day_offset=-1))      # 已满 21 岁
    assert pass_card_filters(young, {"age_min": 20}) is False
    assert pass_card_filters(boundary, {"age_min": 20}) is True
    assert pass_card_filters(old, {"age_min": 20}) is True


def test_age_range_combined():
    ok = _fields(_shifted_birthday(28, day_offset=-1))
    too_young = _fields(_shifted_birthday(20, day_offset=-1))
    too_old = _fields(_shifted_birthday(36, day_offset=-1))
    f = {"age_min": 25, "age_max": 30}
    assert pass_card_filters(ok, f) is True
    assert pass_card_filters(too_young, f) is False
    assert pass_card_filters(too_old, f) is False


def test_age_filter_excludes_user_without_birthday():
    assert pass_card_filters({}, {"age_max": 30}) is False
    assert pass_card_filters({}, {"age_min": 18}) is False


def test_age_params_as_strings_work():
    f = {"age_min": "25", "age_max": "30"}
    assert pass_card_filters(_fields(_shifted_birthday(28, day_offset=-1)), f) is True
    assert pass_card_filters(_fields(_shifted_birthday(35, day_offset=-1)), f) is False


def test_age_invalid_params_ignored_not_crash():
    bad = _fields(_shifted_birthday(35, day_offset=-1))
    for val in ("abc", "nan", "inf", True, [], {}):
        assert pass_card_filters(bad, {"age_max": val}) is True
        assert pass_card_filters(bad, {"age_min": val}) is True
    assert pass_card_filters({}, {"age_max": "abc"}) is True  # 无效即视为未填，无生日也不拦


def test_age_filter_pre1970_negative_timestamp():
    fields = {"生日": -1210752000000}  # 1931-08-21（负毫秒时间戳）
    age = _age_of("1931-08-21")
    assert age > 60
    assert pass_card_filters(fields, {"age_max": age}) is True
    assert pass_card_filters(fields, {"age_max": age - 1}) is False
    assert pass_card_filters(fields, {"age_min": age}) is True
    assert pass_card_filters(fields, {"age_min": age + 1}) is False


def test_age_filter_seconds_timestamp_format():
    fields = {"生日": 946684800}  # 2000-01-01（秒时间戳）
    age = _age_of("2000-01-01")
    assert pass_card_filters(fields, {"age_max": age}) is True
    assert pass_card_filters(fields, {"age_max": age - 1}) is False
    assert pass_card_filters(fields, {"age_min": age}) is True
    assert pass_card_filters(fields, {"age_min": age + 1}) is False


def test_age_filter_future_birthday():
    future = datetime.date.today().replace(year=datetime.date.today().year + 1)
    fields = {"生日": calendar.timegm(future.timetuple()) * 1000}
    assert pass_card_filters(fields, {"age_min": 0}) is False  # 周岁为负
    assert pass_card_filters(fields, {"age_max": 0}) is True
    assert pass_card_filters(fields, {"age_max": 30}) is True


def test_age_filter_leap_day_birthday():
    fields = _fields("2000-02-29")
    age = _age_of("2000-02-29")
    assert pass_card_filters(fields, {"age_max": age}) is True
    assert pass_card_filters(fields, {"age_max": age - 1}) is False


def test_age_min_greater_than_max_no_match():
    f = {"age_min": 40, "age_max": 20}
    assert pass_card_filters(_fields(_shifted_birthday(30, day_offset=-1)), f) is False


def test_birthday_malformed_string_no_crash():
    assert pass_card_filters({"生日": "垃圾数据"}, {"age_max": 30}) is False
    assert pass_card_filters({"生日": "1990"}, {"age_max": 30}) is False
