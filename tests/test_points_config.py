"""麦穗配置单测：默认值、运行时覆盖、校验、降级（不联网）。"""
import pytest

from lib import points_config


def test_defaults_come_from_code(points_db):
    assert points_config.get("invite_reward_female") == 20
    assert points_config.get("invite_reward_male") == 15
    assert points_config.get("redeem_real_like") == 20
    assert points_config.get("invite_confirm_days") == 7
    assert points_config.get("fee_discount_rate") == 0.7
    assert points_config.get("priority_ratio") == 0.3


def test_every_configured_number_is_present(points_db):
    """需求点名的分值/价格/比例/期限，一个都不能漏在代码里。"""
    need = {"invite_reward_female", "invite_reward_male",
            "redeem_real_like", "redeem_priority_signup", "redeem_wish",
            "redeem_matchmaker", "redeem_fee_discount",
            "fee_discount_rate", "priority_ratio",
            "assist_min", "assist_max",
            "invite_confirm_days", "matchmaker_deadline_days",
            "priority_cancel_hours"}
    assert need <= set(points_config.SPECS)


def test_an_override_is_not_written_until_it_is_set(points_db):
    assert points_config.is_overridden("invite_reward_female") is False
    rows = points_db.connection().execute("SELECT count(*) AS n FROM config").fetchone()
    assert rows["n"] == 0


def test_set_value_overrides_and_survives_a_cache_reload(points_db):
    points_config.set_value("invite_reward_female", 25, operator_oid="ou_admin")
    assert points_config.get("invite_reward_female") == 25
    points_config.invalidate()            # 模拟另一个进程/下一次读
    assert points_config.get("invite_reward_female") == 25
    assert points_config.is_overridden("invite_reward_female") is True


def test_only_the_changed_key_is_stored(points_db):
    """只存改过的项。把默认值也写进库的话，以后调默认值就调不动了。"""
    points_config.set_value("invite_reward_female", 25)
    keys = [r["key"] for r in points_db.connection().execute(
        "SELECT key FROM config").fetchall()]
    assert keys == ["invite_reward_female"]
    assert points_config.get("invite_reward_male") == 15


def test_set_value_records_who_and_when(points_db):
    points_config.set_value("assist_max", 40, operator_oid="ou_admin")
    row = points_db.connection().execute(
        "SELECT value, operator_oid, updated_at FROM config WHERE key='assist_max'").fetchone()
    assert row["value"] == "40"
    assert row["operator_oid"] == "ou_admin"
    assert row["updated_at"]


def test_reset_restores_the_default(points_db):
    points_config.set_value("invite_reward_female", 25)
    assert points_config.reset("invite_reward_female") == 20
    assert points_config.get("invite_reward_female") == 20
    assert points_config.is_overridden("invite_reward_female") is False


# ---------------------------------------------------------------- 类型与范围

def test_a_number_given_as_text_is_accepted(points_db):
    """管理员是在飞书里敲指令的，敲进来的永远是字符串。"""
    assert points_config.set_value("assist_max", "40") == 40
    assert points_config.get("assist_max") == 40


def test_a_float_given_as_text_is_accepted(points_db):
    assert points_config.set_value("priority_ratio", "0.25") == 0.25
    assert points_config.get("priority_ratio") == 0.25


def test_a_non_number_is_refused(points_db):
    with pytest.raises(ValueError):
        points_config.set_value("assist_max", "四十")
    assert points_config.get("assist_max") == 50      # 没有半个字节落库


def test_a_value_out_of_range_is_refused(points_db):
    with pytest.raises(ValueError, match="超出范围"):
        points_config.set_value("priority_ratio", 1.5)
    with pytest.raises(ValueError, match="超出范围"):
        points_config.set_value("invite_reward_female", -1)
    assert points_config.get("priority_ratio") == 0.3


def test_an_unknown_key_is_refused(points_db):
    with pytest.raises(KeyError):
        points_config.get("redeem_什么")
    with pytest.raises(KeyError):
        points_config.set_value("redeem_什么", 1)


# ---------------------------------------------------------------- 跨项校验

def test_assist_floor_above_the_ceiling_is_refused(points_db):
    with pytest.raises(ValueError, match="下限大于上限"):
        points_config.set_value("assist_min", 60)


def test_a_cap_below_the_single_shot_max_is_refused(points_db):
    """单次上限 50、同活动累计上限 30 的话，单次上限永远够不着——运营会
    以为自己配错了，其实是这两个数打架。"""
    with pytest.raises(ValueError, match="累计上限"):
        points_config.set_value("assist_activity_cap", 30)


def test_a_zero_priority_ratio_is_refused(points_db):
    with pytest.raises(ValueError, match="优先名额比例"):
        points_config.set_value("priority_ratio", 0)


def test_valid_combinations_still_pass(points_db):
    points_config.set_value("assist_activity_cap", 200)
    points_config.set_value("assist_max", 60)
    points_config.set_value("assist_min", 60)
    assert points_config.get("assist_min") == 60
    assert points_config.get("assist_max") == 60


# ---------------------------------------------------------------- 降级与展示

def test_a_corrupted_value_falls_back_to_the_default(points_db):
    """手工改库改坏了，不能让对账线程崩在一条配置上。"""
    points_db.connection().execute(
        "INSERT INTO config(key, value, updated_at) VALUES('invite_reward_female','二十','t')")
    points_config.invalidate()
    assert points_config.get("invite_reward_female") == 20


def test_reading_config_without_a_database_falls_back_to_defaults(points_db, monkeypatch):
    """bot 刚启动、迁移还没跑的那一瞬间也要能读配置。"""
    points_config.invalidate()

    def _boom():
        raise RuntimeError("库还没建好")

    monkeypatch.setattr(points_db, "connection", _boom)
    assert points_config.get("invite_reward_female") == 20
    assert points_config.get_all()["redeem_wish"] == 30


def test_get_all_covers_every_key(points_db):
    points_config.set_value("redeem_wish", 35)
    allv = points_config.get_all()
    assert set(allv) == set(points_config.SPECS)
    assert allv["redeem_wish"] == 35


def test_describe_marks_what_was_changed(points_db):
    points_config.set_value("redeem_wish", 35, operator_oid="ou_admin")
    rows = {r["key"]: r for r in points_config.describe()}
    assert rows["redeem_wish"]["value"] == 35
    assert rows["redeem_wish"]["default"] == 30
    assert rows["redeem_wish"]["overridden"] is True
    assert rows["redeem_real_like"]["overridden"] is False


def test_the_change_is_visible_to_another_process_within_the_ttl(points_db):
    """bot 改的配置，H5 那个进程最迟 30 秒后要看到——所以缓存必须有 TTL，
    不能只在进程内 invalidate。"""
    assert points_config.CACHE_TTL_SECONDS <= 60
    points_config.set_value("redeem_wish", 35)
    points_config.invalidate()          # 等价于「另一个进程的缓存过期了」
    assert points_config.get("redeem_wish") == 35
