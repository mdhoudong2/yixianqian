#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试服「一对一」H5 API 端到端测试（只允许在 /opt/yixianqian-test 执行，H5 venv）。

只依赖 H5 侧模块（app / config / bitable），不 import bot 模块——bot 与 H5 各有一份
local_config（bot 用 ASSISTANT_APP_ID，H5 用 FEISHU_APP_ID + MESSAGE_TABLE_ID 等），
同进程混用会读到错误凭证。匹配本身已由 seed_test_onetoone.py + 单测覆盖，这里只验
H5 6 个路由 + 留言→已聊推导：

- GET  /api/activities/<id>/onetoone/candidates（收集中：异性列表、排除自己）
- POST /api/activities/<id>/onetoone/select（提交 7 志愿）
- GET  /api/activities/<id>/onetoone/status（状态 + 我的选择）
- GET  /api/activities/<id>/onetoone/result（已完成：必聊名单 + chatted 由留言推导）
- GET  /api/activities/mine/onetoone / .../flag（入口显隐）
- POST /api/messages → 再次读 result 验证对方变「已聊」

状态编排直接改活动表「一对一状态」字段（收集中→已完成），不清结果表（结果由 seed 造好）。
断言全部数据无关（不硬编码人数），只验证路由语义：候选=全量异性且不含自己、志愿回读一致、
结果排名 1..N 连续、留言后目标必聊对象变「已聊」。
"""
import os
import sys

_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))
_EXPECTED_ROOT = "/opt/yixianqian-test"
if os.path.realpath(_ROOT) != _EXPECTED_ROOT:
    print(f"⛔ 仓库根必须是 {_EXPECTED_ROOT}，实际 {os.path.realpath(_ROOT)}")
    sys.exit(1)
if os.environ.get("YIXIANQIAN_ENV", "prod") != "dev":
    print("⛔ 必须 YIXIANQIAN_ENV=dev（本脚本只测测试服）")
    sys.exit(1)

# 关键：web/backend 在前，让 H5 的 config/bitable 读到 web/backend/local_config.py
for _p in (os.path.join(_ROOT, "web", "backend"), _ROOT):
    sys.path.insert(0, _p)

import config as CFG  # noqa: E402
import app as h5_mod  # noqa: E402

bitable = h5_mod.bitable
ACT = "A-0011"
flask_app = h5_mod.app

_issues = []


def _fail(msg):
    _issues.append(msg)
    print(f"  ⛔ {msg}")


def _t(fields, name):
    return bitable.get_field_text(fields, name)


def _find_activity_record():
    for a in bitable.get_activities():
        if _t(a.get("fields", {}), CFG.F_ACTIVITY_ID) == ACT:
            return a
    return None


def _find_participant(gender_ch):
    signups = bitable.get_signups(ACT)
    s_oids = {_t(s.get("fields", {}), CFG.F_SIGNUP_OPENID) for s in signups}
    for u in bitable.get_all_users():
        f = u.get("fields", {})
        oid = _t(f, CFG.F_FEISHU_ID)
        if oid in s_oids and bitable.get_select_value(f, CFG.F_GENDER) == gender_ch:
            return oid, _t(f, CFG.F_USER_ID), _t(f, CFG.F_NICKNAME)
    return None, None, None


def _count_opposite_signups(me_oid, opposite_gender):
    """报名里（排除自己）异性人数——与 candidates 路由同口径，用于断言候选数量。"""
    signups = bitable.get_signups(ACT)
    n = 0
    for s in signups:
        s_oid = _t(s.get("fields", {}), CFG.F_SIGNUP_OPENID)
        if not s_oid or s_oid == me_oid:
            continue
        u = bitable.find_user_by_openid(s_oid)
        if u and bitable.get_select_value(u.get("fields", {}), CFG.F_GENDER) == opposite_gender:
            n += 1
    return n


def main():
    act_record = _find_activity_record()
    if not act_record:
        _fail(f"活动表中找不到 {ACT}")
        return
    act_record_id = act_record["record_id"]

    male_oid, male_uid, male_nick = _find_participant("男性")
    female_oid, female_uid, female_nick = _find_participant("女性")
    if not male_oid or not female_oid:
        _fail(f"找不到参与者：男={male_uid} 女={female_uid}")
        return
    print(f"[参与者] 男 {male_uid} {male_nick} ({male_oid[:10]}…) / "
          f"女 {female_uid} {female_nick} ({female_oid[:10]}…)")

    client = flask_app.test_client()

    def set_session(oid):
        client.set_cookie("yxq_session", h5_mod.create_session(oid, "user"))

    def get(path):
        return client.get(path)

    def post(path, data):
        return client.post(path, json=data, headers={"X-Requested-With": "XMLHttpRequest"})

    def set_status(status):
        bitable.update_record(CFG.ACTIVITY_TABLE_ID, act_record_id,
                              {CFG.F_ACTIVITY_ONETOONE_STATUS: status})
        h5_mod.refresh_snapshot_table("activities")

    h5_mod.refresh_snapshot()

    # ---------- Phase 1: 收集中（candidates / select / status） ----------
    print("\n[1] 收集中：candidates / select / status")
    set_status("收集中")

    set_session(male_oid)
    r = get(f"/api/activities/{ACT}/onetoone/candidates")
    if r.status_code != 200:
        _fail(f"candidates HTTP {r.status_code}: {r.get_json()}")
    else:
        cand = r.get_json().get("candidates", [])
        expected = _count_opposite_signups(male_oid, "女性")
        if any(c["openid"] == male_oid for c in cand):
            _fail("candidates 含自己")
        if len(cand) != expected:
            _fail(f"candidates 数量 {len(cand)} 与异性报名 {expected} 不符")
        if len(cand) < 7:
            _fail(f"candidates 不足 7 位，无法提交志愿：{len(cand)}")
        print(f"  candidates: {len(cand)} 位异性、排除自己（异性报名 {expected}）")

        choices = [c["openid"] for c in cand[:7]]
        r2 = post(f"/api/activities/{ACT}/onetoone/select", {"choices": choices})
        if r2.status_code != 200:
            _fail(f"select HTTP {r2.status_code}: {r2.get_json()}")
        else:
            h5_mod.refresh_snapshot_table("onetoone_selections")
            r3 = get(f"/api/activities/{ACT}/onetoone/status")
            st = r3.get_json()
            if not st.get("my_selected"):
                _fail("status.my_selected 应为 true")
            if st.get("my_choices") != choices:
                _fail(f"status.my_choices 与提交不一致：{st.get('my_choices')} vs {choices}")
            print(f"  select 7 志愿 + status 复核通过（my_choices={len(st.get('my_choices', []))}）")

    # ---------- Phase 2: 已完成（result / mine / flag） ----------
    print("\n[2] 已完成：result / mine/onetoone / flag")
    set_status("已完成")

    r = get(f"/api/activities/{ACT}/onetoone/result")
    partners = []
    if r.status_code != 200:
        _fail(f"result HTTP {r.status_code}: {r.get_json()}")
    else:
        partners = r.get_json().get("partners", [])
        if not partners:
            _fail("result partners 为空")
        ranks = [p.get("rank") for p in partners]
        if ranks != list(range(1, len(partners) + 1)):
            _fail(f"result rank 非 1..N：{ranks}")
        if any(not p.get("openid") for p in partners):
            _fail("result 存在空 openid")
        n_chatted = sum(1 for p in partners if p.get("chatted"))
        print(f"  result: {len(partners)} 位必聊、rank 1..{len(partners)}、初始已聊 {n_chatted} 位")

    r = get("/api/activities/mine/onetoone")
    if r.status_code != 200:
        _fail(f"mine/onetoone HTTP {r.status_code}: {r.get_json()}")
    else:
        acts = r.get_json().get("activities", [])
        if not any(a.get("my_choice_count") == 7 for a in acts):
            _fail(f"mine/onetoone 应含 my_choice_count=7 的活动：{acts}")
        print(f"  mine/onetoone: {len(acts)} 个活动，含本次 7 志愿")

    r = get("/api/activities/mine/onetoone/flag")
    if r.status_code != 200:
        _fail(f"mine/onetoone/flag HTTP {r.status_code}: {r.get_json()}")
    elif r.get_json().get("has_onetoone_activity") is not True:
        _fail(f"mine/onetoone/flag 应为 true：{r.get_json()}")
    else:
        print("  mine/onetoone/flag: has_onetoone_activity=true")

    # ---------- Phase 3: 留言 → 已聊 ----------
    print("\n[3] 留言 → 已聊")
    if not partners:
        _fail("无 partners，跳过留言验证")
    else:
        target = next((p["openid"] for p in partners if not p.get("chatted")), None)
        if not target:
            print("  所有 partner 均已聊（历史留言残留），跳过留言→已聊验证")
        else:
            import time as _time
            r = post("/api/messages", {"target_openid": target,
                                       "content": f"E2E一对一已聊测试 {int(_time.time())}"})
            if r.status_code != 200:
                _fail(f"POST /api/messages HTTP {r.status_code}: {r.get_json()}")
            else:
                h5_mod.refresh_snapshot_table("messages")
                r = get(f"/api/activities/{ACT}/onetoone/result")
                by_oid = {p["openid"]: p for p in r.get_json().get("partners", [])}
                if by_oid.get(target, {}).get("chatted") is not True:
                    _fail(f"留言后 {target[:10]}… 应为已聊")
                else:
                    print(f"  留言后 {target[:10]}… 已变「已聊」")

    print("\n" + ("✅ H5 API 端到端全部通过" if not _issues else "⛔ 存在失败项"))
    if _issues:
        sys.exit(1)


if __name__ == "__main__":
    main()
