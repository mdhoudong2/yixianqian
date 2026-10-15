#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试服「一对一」H5 API 端到端测试（只允许在 /opt/yixianqian-test 执行，H5 venv）。

只依赖 H5 侧模块（app / config / bitable），不 import bot 模块——bot 与 H5 各有一份
local_config（bot 用 ASSISTANT_APP_ID，H5 用 FEISHU_APP_ID + MESSAGE_TABLE_ID 等），
同进程混用会读到错误凭证。匹配本身已由 seed_test_onetoone.py + 单测覆盖，这里只验
H5 4 个路由 + 留言→已聊推导：

- GET  /api/activities/<id>/onetoone/candidates（异性列表、排除自己；志愿随时可改、不看状态）
- POST /api/activities/<id>/onetoone/select（提交 7 志愿）
- GET  /api/activities/<id>/onetoone/status（状态 + 我的选择）
- GET  /api/activities/<id>/onetoone/result（已完成：必聊名单 + chatted 由留言推导）
- POST /api/messages → 再次读 result 验证对方变「已聊」

状态编排直接改活动表「一对一状态」字段：状态模型为「空/已完成」，整段运行期间置
「已完成」（结果端点要求；candidates/select/status 志愿随时可改、不看状态），
不清结果表（结果由 seed 造好）。
**收尾自清理**（审核修复）：本脚本创建的测试留言逐条删掉；活动「一对一状态」改回原值。
否则残留的 `E2E一对一已聊测试…` 留言会污染 chatted 推导、残留状态会改变验收现场。
断言全部数据无关（不硬编码人数），只验证路由语义：候选=全量异性且不含自己、志愿回读一致、
结果排名 1..N 连续、留言后目标必聊对象变「已聊」。
"""
import os
import sys
import time

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
    orig_status = _t(act_record.get("fields", {}), CFG.F_ACTIVITY_ONETOONE_STATUS)

    male_oid, male_uid, male_nick = _find_participant("男性")
    female_oid, female_uid, female_nick = _find_participant("女性")
    if not male_oid or not female_oid:
        _fail(f"找不到参与者：男={male_uid} 女={female_uid}")
        return
    print(f"[参与者] 男 {male_uid} {male_nick} ({male_oid[:10]}…) / "
          f"女 {female_uid} {female_nick} ({female_oid[:10]}…)")

    client = flask_app.test_client()
    created_messages = []  # [(record_id, author_oid)]：收尾逐条删除，防污染 chatted 推导

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

    try:
        h5_mod.refresh_snapshot()

        # ---------- Phase 1: 已完成（candidates / select / status；志愿随时可改，不看状态） ----------
        print("\n[1] 已完成：candidates / select / status")
        set_status("已完成")

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
            if not cand:
                _fail("candidates 为空，无法提交志愿")
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
                print(f"  select {len(choices)} 志愿 + status 复核通过（my_choices={len(st.get('my_choices', []))}）")

        # ---------- Phase 2: 已完成（result；状态已在 Phase 1 置好） ----------
        print("\n[2] 已完成：result")

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

        # ---------- Phase 3: 留言 → 已聊 ----------
        print("\n[3] 留言 → 已聊")
        if not partners:
            _fail("无 partners，跳过留言验证")
        else:
            target = next((p["openid"] for p in partners if not p.get("chatted")), None)
            if not target:
                print("  所有 partner 均已聊（历史留言残留），跳过留言→已聊验证")
            else:
                r = post("/api/messages", {"target_openid": target,
                                           "content": f"E2E一对一已聊测试 {int(time.time())}"})
                if r.status_code != 200:
                    _fail(f"POST /api/messages HTTP {r.status_code}: {r.get_json()}")
                else:
                    # 追踪本轮创建的留言，收尾删除（防「内容前缀」留言污染后续 chatted 推导）
                    mid = (r.get_json() or {}).get("id")
                    if mid:
                        created_messages.append((mid, male_oid))
                    h5_mod.refresh_snapshot_table("messages")
                    r = get(f"/api/activities/{ACT}/onetoone/result")
                    by_oid = {p["openid"]: p for p in r.get_json().get("partners", [])}
                    if by_oid.get(target, {}).get("chatted") is not True:
                        _fail(f"留言后 {target[:10]}… 应为已聊")
                    else:
                        print(f"  留言后 {target[:10]}… 已变「已聊」")
    finally:
        # 收尾自清理：删掉本脚本创建的测试留言 + 活动状态改回原值
        for mid, author in created_messages:
            set_session(author)
            try:
                client.delete(f"/api/messages/{mid}")
            except Exception as e:
                _fail(f"清理测试留言失败 {mid}: {e}")
        if created_messages:
            h5_mod.refresh_snapshot_table("messages")
        set_status(orig_status)
        print(f"[清理] 测试留言已删除 {len(created_messages)} 条；"
              f"活动状态还原为「{orig_status}」")

    print("\n" + ("✅ H5 API 端到端全部通过" if not _issues else "⛔ 存在失败项"))
    if _issues:
        sys.exit(1)


if __name__ == "__main__":
    main()
