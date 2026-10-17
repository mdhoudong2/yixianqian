"""志愿跨轮差异约束（2026-10-16）：必须恰好 7 人 + 行轮次 < R 时至少换 4 人。

口径（用户需求原文）：
- 提交必须恰好 7 个志愿，未满/超出 → 400 SELECT_NOT_FULL；
- R = 该活动已执行的最新轮次（分组结果表与一对一结果表轮次最大值）+ 1，fresh 读；
- 行存在 且 行轮次 < R 且 行内至少 1 个志愿 → 与「本人的该行」比较：
  新志愿与旧志愿至少 4 人不同，不足 → 400 SELECT_ROUND_DIFF；
- 行不存在 / 行轮次 == R（同轮内反复改）/ 行内无志愿 → 无差异约束；
- 保存成功写「轮次」= R；旧数据行轮次空 → 视为第 1 轮；
- R 读失败 → 本次跳过差异校验、轮次沿用旧值（无旧行则 1），绝不因读失败挡住提交。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from tests.test_onetoone_api import (  # noqa: E402
    ACT,
    ME,
    FakeBitable,
)

GS_TBL = "tbl_test_select_rounds"
GR_TBL = "tbl_test_select_rounds_gr"
OTO_RESULT_TBL = "tbl_test_select_rounds_oto"


def _user(oid, uid, gender, nick, status="单身"):
    return {"飞书用户ID": oid, "用户ID": uid, "性别": gender, "昵称": nick,
            "账号状态": status}


USERS = {
    ME: _user(ME, "U-0001", "男性", "小北"),
    "ou_m2": _user("ou_m2", "U-0002", "男性", "阿北"),
    "ou_f1": _user("ou_f1", "U-0003", "女性", "阿南"),
    "ou_f2": _user("ou_f2", "U-0004", "女性", "小西"),
    "ou_f3": _user("ou_f3", "U-0005", "女性", "小东"),
    "ou_f4": _user("ou_f4", "U-0006", "女性", "小南"),
    "ou_f5": _user("ou_f5", "U-0007", "女性", "小中"),
    "ou_f6": _user("ou_f6", "U-0008", "女性", "小六"),
    "ou_f7": _user("ou_f7", "U-0009", "女性", "小七"),
}
FEMME_SEVEN = ["ou_f1", "ou_f2", "ou_f3", "ou_f4", "ou_f5", "ou_f6", "ou_f7"]
GS_CHOICES = [f"第{i}志愿" for i in range(1, 8)]


def _act():
    return {"record_id": "rec_act", "fields": {
        "活动ID": ACT, "活动名称": "关系自洽",
        "分组状态": "收集中", "分组功能开启": "否",
        "一对一状态": "未开始", "一对一功能开启": "否"}}


def _signup(oid):
    return {"record_id": f"rec_s_{oid}", "fields": {
        "活动ID": ACT, "报名人open_id": oid,
        "报名人昵称": USERS.get(oid, {}).get("昵称", "某人"), "状态": "已报名"}}


class Api:
    """假表 + 快照桩 + 结果表 fresh 直读桩（可注入读失败）；校验逻辑全跑真实现。"""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(app, "GROUP_SELECT_TABLE", GS_TBL)
        monkeypatch.setattr(app.bitable, "GROUP_SELECT_TABLE", GS_TBL)
        monkeypatch.setattr(app, "GROUP_RESULT_TABLE", GR_TBL)
        monkeypatch.setattr(app.bitable, "GROUP_RESULT_TABLE", GR_TBL)
        monkeypatch.setattr(app, "ONETOONE_RESULT_TABLE", OTO_RESULT_TBL)
        monkeypatch.setattr(app.bitable, "ONETOONE_RESULT_TABLE", OTO_RESULT_TBL)
        app._rate_store.clear()
        self.refresh_calls = []
        self.snapshot = {"activities": [], "signups": [], "group_selections": [],
                         "users": [], "observers": []}
        monkeypatch.setattr(app, "_snap_live", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "_snap", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "_snap_fresh", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "refresh_snapshot_table_async", self.refresh_calls.append)
        monkeypatch.setattr(app, "snap_self_user",
                            lambda: {"record_id": "rec_u_me", "fields": dict(USERS[ME])})
        monkeypatch.setattr(
            app, "snap_find_user_by_openid",
            lambda oid: ({"record_id": "rec_u_" + oid, "fields": dict(USERS[oid])}
                         if oid in USERS else None))
        self.fake = None
        # 结果表 fresh 直读：默认空表；raw_fail=True 模拟飞书读失败（None）
        self.result_rows = {GR_TBL: [], OTO_RESULT_TBL: []}
        self.raw_fail = False

        def _raw(table_id, filter_conditions=None, *a, **k):
            if self.raw_fail:
                return None
            return [{"record_id": f"rec_{table_id}_{i}", "fields": dict(f)}
                    for i, f in enumerate(self.result_rows.get(table_id, []))]

        monkeypatch.setattr(app.bitable, "raw_search_records", _raw)
        self.client = app.app.test_client()
        self.client.set_cookie("yxq_session", app.create_session(ME))

    def install(self):
        signups = [_signup(oid) for oid in [ME] + FEMME_SEVEN]
        self.fake = FakeBitable({
            app.bitable.ACTIVITY_TABLE_ID: [_act()],
            app.bitable.SIGNUP_TABLE_ID: signups,
            GS_TBL: [],
        })
        self.monkeypatch.setattr(app.bitable, "search_records", self.fake.search_records)
        self.monkeypatch.setattr(app, "_grouping_client", self.fake)
        self.monkeypatch.setattr(app.bitable, "create_record", self.fake.create_record)
        self.monkeypatch.setattr(app.bitable, "update_record", self.fake.update_record)
        self.snapshot["activities"] = [_act()]
        self.snapshot["signups"] = list(signups)
        self.snapshot["group_selections"] = []
        return self.fake

    # ---- 桩数据 ----

    def seed_row(self, choices=(), round_no=None):
        """给本人造一行旧志愿（快照与假表同引用，后续 update 两处都可见）。"""
        fields = {"活动ID": ACT, "选择人open_id": ME, "选择人性别": "男性"}
        for i, cf in enumerate(GS_CHOICES):
            fields[cf] = choices[i] if i < len(choices) else ""
        if round_no is not None:
            fields["轮次"] = round_no
        row = {"record_id": "rec_gs_me", "fields": fields}
        self.fake.tables[GS_TBL] = [row]
        self.snapshot["group_selections"] = [row]
        return row

    def seed_executed_round(self, round_no, table="group"):
        """给活动造一条已执行结果行（轮次表）；默认为分组结果表。"""
        key = GR_TBL if table == "group" else OTO_RESULT_TBL
        self.result_rows[key] = [{"活动ID": ACT, "轮次": str(round_no)}]

    # ---- 请求 ----

    def get(self, path):
        return self.client.get(path)

    def post(self, choices):
        return self.client.post(
            f"/api/activities/{ACT}/group/select",
            json={} if choices is None else {"choices": choices},
            headers={"X-Requested-With": "XMLHttpRequest"})

    def writes(self):
        return [c for c in self.fake.writes() if c[0] in ("create", "update")]


@pytest.fixture
def api(monkeypatch):
    a = Api(monkeypatch)
    a.install()
    return a


# ---------------------------------------------------------------- a. 恰好 7 人

def test_six_rejected_seven_ok(api):
    r6 = api.post(FEMME_SEVEN[:6])
    assert r6.status_code == 400, r6.get_json()
    assert r6.get_json()["code"] == "SELECT_NOT_FULL"
    assert r6.get_json()["error"] == "请选满7个志愿（当前已选 6 个）"
    assert api.writes() == [], "未满 7 人必须零写入"

    r7 = api.post(FEMME_SEVEN)
    assert r7.status_code == 200, r7.get_json()
    assert len([c for c in api.writes() if c[0] == "create"]) == 1


# ---------------------------------------------------------------- b. R=1 与跨轮换人

def test_round_one_submit_ok_and_stamps_round_one(api):
    """无已执行轮次 → R=1；旧数据行（轮次空=第 1 轮）与 R 同行 → 无差异约束。"""
    api.seed_row(choices=FEMME_SEVEN, round_no=None)
    r = api.post(FEMME_SEVEN)   # 一人未换也放行（R=1 不比较）
    assert r.status_code == 200, r.get_json()
    row = api.fake.tables[GS_TBL][0]
    assert row["fields"]["轮次"] == 1, "保存成功须写 轮次=R=1"


def test_round_two_rejects_only_three_changes(api):
    """已执行第 1 轮 + 旧行 4 人：新 7 人与旧行重叠 4（只换 3）→ 400、零写入。"""
    api.seed_row(choices=FEMME_SEVEN[:4], round_no=None)   # 旧数据行，轮次空=1
    api.seed_executed_round(1)                             # R=2
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["code"] == "SELECT_ROUND_DIFF"
    assert "只换了 3 人" in r.get_json()["error"]
    assert api.writes() == [], "被差异校验拒绝必须零写入"


def test_round_two_accepts_four_changes_and_stamps_round(api):
    """同场景换 4 人 → 200，且行「轮次」写到 R=2。"""
    api.seed_row(choices=FEMME_SEVEN[:3], round_no=1)      # 旧行 3 人
    api.seed_executed_round(1)                             # R=2
    r = api.post(FEMME_SEVEN)                              # 新减旧 = f4..f7 = 4 人
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["select_round"] == 2
    row = api.fake.tables[GS_TBL][0]
    assert row["fields"]["轮次"] == 2, "保存成功须写 轮次=R"
    assert row["fields"]["第7志愿"] == "ou_f7"


def test_latest_executed_round_uses_both_result_tables(api):
    """R 的输入 = 分组结果表与一对一结果表轮次的最大值（含遗留空轮次=1）。"""
    api.result_rows[GR_TBL] = [{"活动ID": ACT, "轮次": "1"}]
    api.result_rows[OTO_RESULT_TBL] = [{"活动ID": ACT, "轮次": "3"}]
    assert app._latest_executed_round(ACT) == 3
    # 其它活动的结果行不算数
    api.result_rows[OTO_RESULT_TBL].append({"活动ID": "A-9999", "轮次": "9"})
    assert app._latest_executed_round(ACT) == 3
    # 一对一遗留行轮次空 = 第 1 轮（round_of 归一化口径）
    api.result_rows[OTO_RESULT_TBL] = [{"活动ID": ACT, "轮次": ""}]
    api.result_rows[GR_TBL] = []
    assert app._latest_executed_round(ACT) == 1
    # 全空 → 0（调用端 +1 得 R=1）
    api.result_rows[OTO_RESULT_TBL] = []
    assert app._latest_executed_round(ACT) == 0


# ---------------------------------------------------------------- c/d. 无约束分支

def test_same_round_edit_is_unconstrained(api):
    """行轮次 == R（同轮内反复改）→ 任意改，不受「至少换 4 人」限制。"""
    api.seed_row(choices=FEMME_SEVEN[:4], round_no=2)
    api.seed_executed_round(1)                             # R=2；行轮次=2
    r = api.post(FEMME_SEVEN)                              # 只换 3 人
    assert r.status_code == 200, r.get_json()
    assert api.fake.tables[GS_TBL][0]["fields"]["轮次"] == 2


def test_no_existing_row_submits_without_constraint(api):
    """无旧行直交 → 无差异约束；保存写 轮次=R。"""
    api.seed_executed_round(2)                             # R=3
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 200, r.get_json()
    assert api.fake.tables[GS_TBL][0]["fields"]["轮次"] == 3


def test_existing_row_without_choices_is_unconstrained(api):
    """行存在但行内无志愿（0 个）→ 无差异约束（旧实现可能留下空行）。"""
    api.seed_row(choices=(), round_no=1)
    api.seed_executed_round(1)                             # R=2
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 200, r.get_json()
    assert api.fake.tables[GS_TBL][0]["fields"]["轮次"] == 2


# ---------------------------------------------------------------- e. R 读失败退化

def test_round_read_failure_skips_diff_and_keeps_old_round(api):
    """R 读不到 → 跳过差异校验（只换 3 人也放行），且不改写旧行轮次。"""
    api.seed_row(choices=FEMME_SEVEN[:4], round_no=1)
    api.raw_fail = True
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 200, r.get_json()
    updates = [c for c in api.writes() if c[0] == "update"]
    assert len(updates) == 1
    assert "轮次" not in updates[0][3], "R 读失败时不得改写轮次（沿用旧值）"
    assert api.fake.tables[GS_TBL][0]["fields"]["轮次"] == 1


def test_round_read_failure_new_row_stamps_first_round(api):
    """R 读失败且无旧行 → 照常创建，轮次记 1。"""
    api.raw_fail = True
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 200, r.get_json()
    assert api.fake.tables[GS_TBL][0]["fields"]["轮次"] == 1


# ---------------------------------------------------------------- 状态端点下发

def test_status_reports_select_round_and_needs_change(api):
    api.seed_row(choices=FEMME_SEVEN[:4], round_no=1)
    api.seed_executed_round(1)                             # R=2；行轮次 1 < 2 且有志愿
    st = api.get(f"/api/activities/{ACT}/group/status").get_json()
    assert st["select_round"] == 2
    assert st["needs_change"] is True
    assert st["my_choices"] == FEMME_SEVEN[:4]


def test_status_needs_change_false_when_same_round_or_empty_choices(api):
    api.seed_executed_round(1)                             # R=2
    # 行轮次 == R：同轮内改过，无需再换
    api.seed_row(choices=FEMME_SEVEN[:4], round_no=2)
    st = api.get(f"/api/activities/{ACT}/group/status").get_json()
    assert st["select_round"] == 2 and st["needs_change"] is False
    # 行内无志愿：即使轮次旧也不要求换
    api.seed_row(choices=(), round_no=1)
    st = api.get(f"/api/activities/{ACT}/group/status").get_json()
    assert st["select_round"] == 2 and st["needs_change"] is False
    # 无行
    api.snapshot["group_selections"] = []
    api.fake.tables[GS_TBL] = []
    st = api.get(f"/api/activities/{ACT}/group/status").get_json()
    assert st["select_round"] == 2 and st["needs_change"] is False
    assert st["my_selected"] is False


def test_status_round_read_failure_returns_null_and_no_tip(api):
    """R 读失败 → select_round:null、needs_change:false（前端不显示提示）。"""
    api.seed_row(choices=FEMME_SEVEN[:4], round_no=1)
    api.raw_fail = True
    st = api.get(f"/api/activities/{ACT}/group/status").get_json()
    assert st["select_round"] is None
    assert st["needs_change"] is False
    assert st["my_selected"] is True
