"""认领全流程回归：限流分桶 / 认领绑定并发防护 / 自动报名 fail-closed / 注册防重 / 原因回传。

覆盖现场「注册→人工审核→机器人认领→自动报名」链路上 web 侧的六处修复：
- FIX-1 认领限流不再按 IP（反代后 remote_addr 恒 127.0.0.1）分桶；
- FIX-2 报名查重/满员 fail-closed（查询失败不得当成「未报名/未满员」）；
- FIX-3 并发认领同一条预填记录时的锁内重读防护；
- FIX-4 注册写库前锁内严格查重（fail-closed）；
- FIX-5 写后刷新 users 快照；
- FIX-6 未自动报名时响应携带可见原因。
全程零真实网络：monkeypatch app.* 与内存假表。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

H = {"X-Requested-With": "XMLHttpRequest"}

SINGLE_PAID = {
    app.F_ACCOUNT_STATUS: "单身",
    app.F_WECHAT_PAYMENT: app.WECHAT_PAY_PAID,
    app.F_NICKNAME: "阿甲",
}


class _Ctx:
    client = None
    refreshed = None


@pytest.fixture
def ctx(monkeypatch):
    """测试客户端：清限流桶、静音会话副作用、记录快照刷新调用。"""
    app._rate_store.clear()
    c = _Ctx()
    c.refreshed = []
    monkeypatch.setattr(app, "touch_last_active", lambda *a, **k: None)
    monkeypatch.setattr(app, "roles_of", lambda oid: {"user"})
    monkeypatch.setattr(app, "refresh_snapshot_table_async", c.refreshed.append)
    c.client = app.app.test_client()
    return c


def _post_claim(client, code, phone, name):
    return client.post("/api/auth/claim",
                       json={"code": code, "phone": phone, "name": name}, headers=H)


def _stub_activity(monkeypatch, status="报名中", max_signup=5):
    monkeypatch.setattr(app, "snap_resolve_activity", lambda aid, fresh=False: (
        {"record_id": "rec_act",
         "fields": {app.F_ACTIVITY_STATUS: status, app.F_ACTIVITY_MAX_SIGNUP: max_signup}},
        "A-0011"))


def _stub_claim_binding(monkeypatch, oid="ou_claim_1", status=""):
    """认领匹配到唯一预填记录所需的全部桩；返回 (rec, updates)。"""
    rec = {"record_id": "rec_p1",
           "fields": {app.F_PHONE: [{"number": "13800000001"}], app.F_REAL_NAME: "测试用户"}}
    if status:
        rec["fields"][app.F_ACCOUNT_STATUS] = status
    monkeypatch.setattr(app, "_exchange_code_for_openid", lambda code: oid)
    monkeypatch.setattr(app, "snap_find_user_by_openid", lambda o: None)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [rec])
    live_fields = dict(rec["fields"])
    monkeypatch.setattr(app.bitable, "get_record",
                        lambda t, r: {"record_id": r, "fields": dict(live_fields)})
    updates = []
    updated = {"record_id": "rec_p1",
               "fields": {**live_fields, app.F_FEISHU_ID: oid}}
    monkeypatch.setattr(app.bitable, "update_record",
                        lambda t, r, f: updates.append((t, r, f)) or updated)
    return rec, updates


# ========== FIX-1 限流分桶 ==========

def test_claim_rate_limit_per_openid_not_shared(ctx, monkeypatch):
    """同一 open_id 第 9 次认领 → 429；另一个 open_id 与自己同一 IP 也不被拖累。"""
    code2oid = {"u1": "ou_rl_1", "u2": "ou_rl_2"}
    monkeypatch.setattr(app, "_exchange_code_for_openid", lambda code: code2oid.get(code))
    monkeypatch.setattr(app, "snap_find_user_by_openid", lambda o: None)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [])

    for _ in range(8):
        assert _post_claim(ctx.client, "u1", "13800000010", "限流用户").status_code == 404
    r9 = _post_claim(ctx.client, "u1", "13800000010", "限流用户")
    assert r9.status_code == 429

    # 第 9 次才拒绝，前 8 次正常走完（含一次同 IP 全服熔断 key）
    keys = list(app._rate_store)
    assert "claim-all:all:/api/auth/claim" in keys
    assert "claim:ou_rl_1:/api/auth/claim" in keys
    assert not any("127.0.0.1" in k for k in keys)

    r_other = _post_claim(ctx.client, "u2", "13800000010", "限流用户")
    assert r_other.status_code == 404  # 换 open_id 不共享身份桶


# ========== FIX-3 认领绑定与并发防护 ==========

def test_claim_binding_happy_path(ctx, monkeypatch):
    """唯一匹配：绑定 open_id、空状态补「待审核」、刷新 users 快照并种会话。"""
    _, updates = _stub_claim_binding(monkeypatch, oid="ou_new")
    monkeypatch.setattr(app, "_claim_auto_signup", lambda o, f: (False, "pending"))
    r = _post_claim(ctx.client, "code-ok", "13800000001", "测试用户")
    assert r.status_code == 200
    data = r.get_json()
    assert data["claimed"] is True
    assert data["auto_signed_up"] is False
    assert "auto_signed_note" not in data  # pending 属正常跳过
    assert updates == [(app.USER_TABLE_ID, "rec_p1",
                        {app.F_FEISHU_ID: "ou_new", app.F_ACCOUNT_STATUS: "待审核"})]
    assert "users" in ctx.refreshed
    assert "yxq_session" in "".join(r.headers.getlist("Set-Cookie"))


def test_claim_binding_keeps_existing_status(ctx, monkeypatch):
    """预填记录已有状态（待审核）时只写 open_id，不改状态。"""
    _, updates = _stub_claim_binding(monkeypatch, oid="ou_new", status="待审核")
    monkeypatch.setattr(app, "_claim_auto_signup", lambda o, f: (False, "pending"))
    r = _post_claim(ctx.client, "code-ok", "13800000001", "测试用户")
    assert r.status_code == 200
    assert updates == [(app.USER_TABLE_ID, "rec_p1", {app.F_FEISHU_ID: "ou_new"})]


def test_claim_recheck_feishu_bound_409(ctx, monkeypatch):
    """锁内重读发现飞书ID已非空 → 409，不 update（并发认领防护）。"""
    _stub_claim_binding(monkeypatch, oid="ou_late")
    monkeypatch.setattr(app.bitable, "get_record", lambda t, r: (
        {"record_id": r, "fields": {app.F_PHONE: [{"number": "13800000001"}],
                                    app.F_REAL_NAME: "测试用户",
                                    app.F_FEISHU_ID: "ou_other"}}))
    upd = []
    monkeypatch.setattr(app.bitable, "update_record", lambda *a: upd.append(a))
    r = _post_claim(ctx.client, "code-ok", "13800000001", "测试用户")
    assert r.status_code == 409
    assert r.get_json()["error"] == "该资料已被认领，请联系现场小天使"
    assert upd == []


def test_claim_reread_failure_502(ctx, monkeypatch):
    """锁内重读失败（None）→ 502，不 update、不当成功。"""
    _stub_claim_binding(monkeypatch, oid="ou_x")
    monkeypatch.setattr(app.bitable, "get_record", lambda t, r: None)
    upd = []
    monkeypatch.setattr(app.bitable, "update_record", lambda *a: upd.append(a))
    r = _post_claim(ctx.client, "code-ok", "13800000001", "测试用户")
    assert r.status_code == 502
    assert upd == []


@pytest.mark.parametrize("payload,status,error_part", [
    ({"code": "", "phone": "13800000001", "name": "测试用户"}, 400, "缺少飞书登录凭证"),
    ({"code": "c", "phone": "12345", "name": "测试用户"}, 400, "11 位手机号"),
    ({"code": "c", "phone": "13800000001", "name": "x"}, 400, "真实姓名"),
])
def test_claim_bad_params_400(ctx, monkeypatch, payload, status, error_part):
    """参数校验：缺 code / 手机号非法 / 姓名非法 → 400（发生在限流与换码之前）。"""
    called = []
    monkeypatch.setattr(app, "_exchange_code_for_openid",
                        lambda code: called.append(code) or "ou_never")
    r = ctx.client.post("/api/auth/claim", json=payload, headers=H)
    assert r.status_code == status
    assert error_part in r.get_json()["error"]
    assert called == []


def test_claim_no_match_404(ctx, monkeypatch):
    """没有手机号+姓名匹配的预填记录 → 404。"""
    monkeypatch.setattr(app, "_exchange_code_for_openid", lambda code: "ou_none")
    monkeypatch.setattr(app, "snap_find_user_by_openid", lambda o: None)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [])
    r = _post_claim(ctx.client, "c", "13800000002", "无此人")
    assert r.status_code == 404


def test_claim_multiple_matches_409(ctx, monkeypatch):
    """同名同号匹配到多条 → 409，不做任何写入。"""
    recs = [
        {"record_id": "rec_a", "fields": {app.F_PHONE: "13800000003", app.F_REAL_NAME: "张三"}},
        {"record_id": "rec_b", "fields": {app.F_PHONE: [{"number": "13800000003"}],
                                          app.F_REAL_NAME: " 张三 "}},
    ]
    monkeypatch.setattr(app, "_exchange_code_for_openid", lambda code: "ou_dup")
    monkeypatch.setattr(app, "snap_find_user_by_openid", lambda o: None)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: recs)
    upd = []
    monkeypatch.setattr(app.bitable, "update_record", lambda *a: upd.append(a))
    r = _post_claim(ctx.client, "c", "13800000003", "张三")
    assert r.status_code == 409
    assert upd == []


def test_claim_update_failure_502(ctx, monkeypatch):
    """锁内 update 失败 → 502，不触发自动报名。"""
    _stub_claim_binding(monkeypatch, oid="ou_fail")
    monkeypatch.setattr(app.bitable, "update_record", lambda *a: None)
    calls = []
    monkeypatch.setattr(app, "_claim_auto_signup", lambda o, f: calls.append(o) or (False, "retry"))
    r = _post_claim(ctx.client, "c", "13800000001", "测试用户")
    assert r.status_code == 502
    assert calls == []


def test_claim_existing_profile_logs_in(ctx, monkeypatch):
    """幂等：已有档案直接登录 claimed=false，不查 pending、不 update。"""
    existing = {"record_id": "rec_e",
                "fields": {app.F_FEISHU_ID: "ou_exist", app.F_ACCOUNT_STATUS: "单身",
                           app.F_NICKNAME: "老用户"}}
    monkeypatch.setattr(app, "_exchange_code_for_openid", lambda code: "ou_exist")
    monkeypatch.setattr(app, "snap_find_user_by_openid", lambda o: existing)
    raw_calls = []
    monkeypatch.setattr(app.bitable, "raw_search_records",
                        lambda *a, **k: raw_calls.append(a) or [])
    upd = []
    monkeypatch.setattr(app.bitable, "update_record", lambda *a: upd.append(a))
    r = _post_claim(ctx.client, "c", "13800000009", "老用户")
    assert r.status_code == 200
    assert r.get_json()["claimed"] is False
    assert raw_calls == []
    assert upd == []


# ========== FIX-2 自动报名 fail-closed ==========

def test_probe_functions_fail_closed(monkeypatch):
    """bitable 两个探测：raw 查询失败 → ok=False，不得伪装成「0 条」。"""
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: None)
    assert app.bitable.user_signup_probe("A-0011", "ou_x") == (False, False)
    assert app.bitable.signup_count_probe("A-0011") == (False, 0)


def test_auto_signup_probe_fail_no_write(ctx, monkeypatch):
    """报名查重探测失败（飞书瞬时故障）→ retry 且绝不 create。"""
    _stub_activity(monkeypatch)
    created = []
    monkeypatch.setattr(app.bitable, "user_signup_probe", lambda a, o: (False, False))
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    assert app._claim_auto_signup("ou_1", dict(SINGLE_PAID)) == (False, "retry")
    assert created == []
    assert ctx.refreshed == []


def test_auto_signup_count_probe_fail_no_write(ctx, monkeypatch):
    """满员计数探测失败 → retry 且绝不 create（不能把失败当 0 人继续写）。"""
    _stub_activity(monkeypatch)
    created = []
    monkeypatch.setattr(app.bitable, "user_signup_probe", lambda a, o: (True, False))
    monkeypatch.setattr(app.bitable, "signup_count_probe", lambda a: (False, 0))
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    assert app._claim_auto_signup("ou_1", dict(SINGLE_PAID)) == (False, "retry")
    assert created == []


def test_auto_signup_create_fail_retry(ctx, monkeypatch):
    """写库失败 → retry（不要假报成功）。"""
    _stub_activity(monkeypatch)
    monkeypatch.setattr(app.bitable, "user_signup_probe", lambda a, o: (True, False))
    monkeypatch.setattr(app.bitable, "signup_count_probe", lambda a: (True, 0))
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: None)
    assert app._claim_auto_signup("ou_1", dict(SINGLE_PAID)) == (False, "retry")


def test_auto_signup_activity_unresolved_retry(ctx, monkeypatch):
    """活动解析不到 → retry（无法区分查询失败/确无活动，宁可不报）。"""
    monkeypatch.setattr(app, "snap_resolve_activity", lambda aid, fresh=False: (None, None))
    created = []
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    assert app._claim_auto_signup("ou_1", dict(SINGLE_PAID)) == (False, "retry")
    assert created == []


def test_auto_signup_happy(ctx, monkeypatch):
    """单身+已缴费+报名中+未报名+未满员 → create 恰一次，返回 (True, 'signed')。"""
    _stub_activity(monkeypatch)
    probes = []
    monkeypatch.setattr(app.bitable, "user_signup_probe",
                        lambda a, o: probes.append(o) or (True, False))
    monkeypatch.setattr(app.bitable, "signup_count_probe", lambda a: (True, 0))
    created = []
    monkeypatch.setattr(app.bitable, "create_record",
                        lambda t, f: created.append((t, f)) or {"record_id": "rec_new"})
    assert app._claim_auto_signup("ou_happy", dict(SINGLE_PAID)) == (True, "signed")
    assert len(created) == 1
    table_id, fields = created[0]
    assert table_id == app.SIGNUP_TABLE_ID
    assert fields == {app.F_SIGNUP_ACTIVITY_ID: "A-0011", app.F_SIGNUP_OPENID: "ou_happy",
                      app.F_SIGNUP_NICKNAME: "阿甲", app.F_SIGNUP_STATUS: "已报名"}
    assert probes == ["ou_happy", "ou_happy"]  # 锁外快速探测 + 锁内复查
    assert "signups" in ctx.refreshed and "activities" in ctx.refreshed


def test_auto_signup_skips_pending_and_not_paid(ctx, monkeypatch):
    """非单身 → pending；未缴费 → not_paid；两者都在解析活动之前短路。"""
    resolved = []
    monkeypatch.setattr(app, "snap_resolve_activity",
                        lambda aid, fresh=False: resolved.append(aid) or (None, None))
    pending_fields = {**SINGLE_PAID, app.F_ACCOUNT_STATUS: "待审核"}
    assert app._claim_auto_signup("ou_p", pending_fields) == (False, "pending")
    unpaid_fields = {**SINGLE_PAID, app.F_WECHAT_PAYMENT: "只注册App"}
    assert app._claim_auto_signup("ou_np", unpaid_fields) == (False, "not_paid")
    assert resolved == []


def test_auto_signup_skips_not_open(ctx, monkeypatch):
    """活动非报名中 → not_open，不探测不写库。"""
    _stub_activity(monkeypatch, status="已结束")
    created = []
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    assert app._claim_auto_signup("ou_1", dict(SINGLE_PAID)) == (False, "not_open")
    assert created == []


def test_auto_signup_skips_full(ctx, monkeypatch):
    """满员（count >= max_signup）→ full，不写库。"""
    _stub_activity(monkeypatch, max_signup=3)
    monkeypatch.setattr(app.bitable, "user_signup_probe", lambda a, o: (True, False))
    monkeypatch.setattr(app.bitable, "signup_count_probe", lambda a: (True, 3))
    created = []
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    assert app._claim_auto_signup("ou_1", dict(SINGLE_PAID)) == (False, "full")
    assert created == []


def test_auto_signup_skips_already(ctx, monkeypatch):
    """已报名 → already，锁外探测即短路，不写库。"""
    _stub_activity(monkeypatch)
    monkeypatch.setattr(app.bitable, "user_signup_probe", lambda a, o: (True, True))
    created = []
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    assert app._claim_auto_signup("ou_1", dict(SINGLE_PAID)) == (False, "already")
    assert created == []


# ========== FIX-6 未自动报名原因回传 ==========

_NOTES = {
    "not_paid": "未查到缴费记录，暂未自动报名",
    "no_activity": "活动暂未开放报名，可稍后在活动页报名",
    "not_open": "活动暂未开放报名，可稍后在活动页报名",
    "full": "活动名额已满，暂未自动报名",
    "retry": "报名服务繁忙，稍后会自动重试",
}


@pytest.mark.parametrize("reason", sorted(_NOTES))
def test_claim_auto_signed_note_present(ctx, monkeypatch, reason):
    """未自动报名且原因可解释时，响应附 auto_signed_note 文案。"""
    _stub_claim_binding(monkeypatch, oid="ou_note")
    monkeypatch.setattr(app, "_claim_auto_signup", lambda o, f: (False, reason))
    r = _post_claim(ctx.client, "c", "13800000001", "测试用户")
    data = r.get_json()
    assert r.status_code == 200
    assert data["auto_signed_up"] is False
    assert data["auto_signed_note"] == _NOTES[reason]


@pytest.mark.parametrize("reason", ["pending", "already"])
def test_claim_auto_signed_note_absent(ctx, monkeypatch, reason):
    """pending/already 属正常跳过，不附 note。"""
    _stub_claim_binding(monkeypatch, oid="ou_note2")
    monkeypatch.setattr(app, "_claim_auto_signup", lambda o, f: (False, reason))
    r = _post_claim(ctx.client, "c", "13800000001", "测试用户")
    assert r.status_code == 200
    assert "auto_signed_note" not in r.get_json()


def test_claim_auto_signed_true_no_note(ctx, monkeypatch):
    """自动报名成功：auto_signed_up=true 且不带 note。"""
    _stub_claim_binding(monkeypatch, oid="ou_ok")
    monkeypatch.setattr(app, "_claim_auto_signup", lambda o, f: (True, "signed"))
    r = _post_claim(ctx.client, "c", "13800000001", "测试用户")
    data = r.get_json()
    assert data["auto_signed_up"] is True
    assert "auto_signed_note" not in data


# ========== FIX-4 注册防重复档案 ==========

def _patch_register_minimal(monkeypatch):
    """把注册规则压到最小（省去构造全量表单），聚焦写库前查重逻辑。"""
    monkeypatch.setattr(app, "_REGISTER_RULES",
                        {"姓名": {"type": 1, "required": True, "options": []}})
    monkeypatch.setattr(app, "_REGISTER_PHOTO_MIN", 0)
    monkeypatch.setattr(app, "_REGISTER_PHOTO_MAX", 0)
    monkeypatch.setattr(app.bitable, "search_records", lambda *a, **k: [])  # 快速预检未命中


def _login(client, oid):
    client.set_cookie("yxq_session", app.create_session(oid, "user"))


def test_register_strict_unit(monkeypatch):
    """_register_user_record_strict：失败 (False, None)；有效状态命中；无效状态不算。"""
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: None)
    assert app._register_user_record_strict("ou_x") == (False, None)

    rows = [
        {"record_id": "rec_old", "fields": {app.F_FEISHU_ID: "ou_x",
                                            app.F_ACCOUNT_STATUS: "已退出"}},
        {"record_id": "rec_ok", "fields": {app.F_FEISHU_ID: "ou_x",
                                           app.F_ACCOUNT_STATUS: "待审核"}},
    ]
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: rows)
    ok, rec = app._register_user_record_strict("ou_x")
    assert ok is True and rec["record_id"] == "rec_ok"

    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [rows[0]])
    assert app._register_user_record_strict("ou_x") == (True, None)


def test_register_strict_fail_502_no_create(ctx, monkeypatch):
    """锁内严格查重失败（raw 查询 None）→ 502 且不 create（fail-closed）。"""
    _patch_register_minimal(monkeypatch)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: None)
    created = []
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    _login(ctx.client, "ou_reg1")
    r = ctx.client.post("/api/register", json={"姓名": "测试丙"}, headers=H)
    assert r.status_code == 502
    assert created == []


def test_register_duplicate_in_lock_409_no_create(ctx, monkeypatch):
    """锁内发现已存在有效档案 → 409 原文案且不 create（并发/重试防护）。"""
    _patch_register_minimal(monkeypatch)
    dup = {"record_id": "rec_dup",
           "fields": {app.F_FEISHU_ID: "ou_reg2", app.F_ACCOUNT_STATUS: "待审核"}}
    monkeypatch.setattr(app, "_register_user_record_strict", lambda o: (True, dup))
    created = []
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: created.append(a))
    _login(ctx.client, "ou_reg2")
    r = ctx.client.post("/api/register", json={"姓名": "测试丙"}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "你已经注册过啦，无需重复注册"
    assert created == []


def test_register_success_refreshes_users(ctx, monkeypatch):
    """注册成功：create 后刷新 users 快照。"""
    _patch_register_minimal(monkeypatch)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [])
    monkeypatch.setattr(app.bitable, "create_record", lambda *a, **k: (
        {"record_id": "rec_new", "fields": {"用户ID": "U-9999"}}))
    _login(ctx.client, "ou_reg3")
    r = ctx.client.post("/api/register", json={"姓名": "测试丙"}, headers=H)
    assert r.status_code == 200
    assert "users" in ctx.refreshed
