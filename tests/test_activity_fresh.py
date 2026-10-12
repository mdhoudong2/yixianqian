"""活动详情 / 报名的活动解析走 fresh（审计 G5 回归）。

管理员直接在 base 新建活动后，H5 快照最坏 300s 才看得到：详情与报名必须实时读，
否则「管理员刚发链接、最先点的人看到活动不存在」。
脚手架复用 test_onetoone_api 的 Api（live/stale 分离的双层快照桩）。
"""
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

from tests.test_onetoone_api import ACT  # noqa: E402
from tests.test_onetoone_api import Api as OtoApi  # noqa: E402


def _new_act(status="报名中"):
    return {"record_id": "rec_act_new", "fields": {
        "活动ID": ACT, "活动名称": "刚建的活动", "活动状态": status,
        "分组状态": "未开始", "分组功能开启": "否",
        "一对一状态": "未开始", "一对一功能开启": "否"}}


def _install_new_only(a, act):
    a.install(activities=[act], stale_activities=[], live_activities=[act],
              signups=[], stale_signups=[])


def test_activity_detail_uses_live_activities(monkeypatch, tmp_path):
    a = OtoApi(monkeypatch, tmp_path)
    _install_new_only(a, _new_act())

    r = a.get(f"/api/activities/{ACT}")
    assert r.status_code == 200, "快照里没有但实时有：详情必须立刻可见"
    assert r.get_json()["name"] == "刚建的活动"


def test_signup_uses_live_activities(monkeypatch, tmp_path):
    a = OtoApi(monkeypatch, tmp_path)
    act = _new_act()
    _install_new_only(a, act)

    r = a.post(f"/api/activities/{ACT}/signup", {})
    assert r.status_code == 200, "快照里没有但实时有：报名必须立刻可用"
    assert r.get_json()["ok"] is True
