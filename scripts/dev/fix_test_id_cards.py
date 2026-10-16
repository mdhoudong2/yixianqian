#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性修复：测试 base 两个种子的身份证缺失（F-14）。

背景：H5 全局门禁 get_id_valid 对普通用户强验「身份证号」（校验位/日期/前缀），
U-0003（历史手工测试号，身份证被填成「无」）与 U-2601（seed_onetoone_100.py 建号，
身份证为空）都过不了，验收时会弹「身份证有误或缺失」。
本脚本只补这两条记录的「身份证号」字段：先读后写、写后回读验证，不碰其他字段。

用法（在 /opt/yixianqian-test 仓库根，必须显式 YIXIANQIAN_ENV=dev）：
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/fix_test_id_cards.py plan    # 只读预览
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/fix_test_id_cards.py run     # 执行修复
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/fix_test_id_cards.py verify  # 只读复核

自建守卫（任一不满足即退出，绝不写入）：
  1) 仓库根 realpath 必须等于 /opt/yixianqian-test；
  2) BASE_TOKEN 必须≠生产 /opt/yixianqian/bot/local_config.py 的值，
     且等于本仓库 bot/local_config.py 文本解析值；
  3) USER_TABLE_ID 必须等于测试 base 已知值（防生产 fallback）；
  4) 必须显式 YIXIANQIAN_ENV=dev（不认 YX_DEV_ALLOW 兜底）。
"""
import os
import re
import sys

_D = os.path.dirname(os.path.abspath(__file__))
if _D not in sys.path:
    sys.path.insert(0, _D)

from _prod_guard import guard as _legacy_guard  # noqa: E402

_legacy_guard(os.path.basename(__file__))

_EXPECTED_ROOT = "/opt/yixianqian-test"
_PROD_CFG_PATH = "/opt/yixianqian/bot/local_config.py"
_EXPECTED_USER_TABLE = "tblMFjHwrTlTqrKZ"
_FIELD_ID_CARD = "身份证号"
_FIELD_UID = "用户ID"
_FIELD_OID = "飞书用户ID"

# record_id / 期望用户ID / open_id 前缀 / 昵称 / 性别 / 生日(生成用) / 顺序段(生成用)
_TARGETS = [
    {"rid": "rec282FmPGzfJT", "uid": "U-0003",
     "oid_prefix": "ou_286ec1c435cb8b5203a1c", "nick": "丸旦",
     "gender": "男性", "birth": "19900115", "seq": 3},
    {"rid": "reczz28LpxsrTXRL", "uid": "U-2601",
     "oid_prefix": "ou_fake_oto_male_001", "nick": "百人男001",
     "gender": "男性", "birth": "19920120", "seq": 1},
]

_ID_REGION = "440305"  # 深圳占位区划（仅测试号码，不指向真实公民）
_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"

C = None
bitable = None
get_field_text = None
validate_id_card = None


def _fail(msg):
    print(f"⛔ {msg}")
    sys.exit(1)


def _mask_id(idc, valid):
    """脱敏展示：只报是否有效与后 4 位。"""
    if not idc:
        return "（空）无效"
    return f"{'有效' if valid else '无效'} …{idc[-4:]}（len={len(idc)}）"


def _parse_base(path):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _fail(f"无法读取 {path}（{exc}）")
    m = re.search(r'^BASE_TOKEN\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else None


def _gen_id_card(gender, birth, seq):
    """生成格式合法的测试身份证号（GB 11643 校验位），第17位男奇女偶。"""
    body = (_ID_REGION + birth + f"{seq % 100:02d}"
            + ("3" if gender == "男性" else "2"))
    return body + _ID_CHECK[sum(int(body[i]) * _ID_WEIGHTS[i] for i in range(17)) % 11]


def _guard_and_import():
    global C, bitable, get_field_text, validate_id_card

    real_root = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if real_root != _EXPECTED_ROOT:
        _fail(f"守卫1 失败：仓库根必须是 {_EXPECTED_ROOT}，实际 {real_root}")
    print(f"✅ 守卫1 仓库根 {real_root}")

    env = os.environ.get("YIXIANQIAN_ENV", "prod")
    if env != "dev":
        _fail(f"守卫4 失败：必须显式 YIXIANQIAN_ENV=dev（当前 {env!r}）")
    print("✅ 守卫4 环境 YIXIANQIAN_ENV=dev")

    sys.path.insert(0, os.path.join(real_root, "bot"))
    sys.path.insert(0, real_root)

    import constants as _constants
    import clients as _clients

    c_file = os.path.realpath(_constants.__file__)
    if not c_file.startswith(real_root + os.sep):
        _fail(f"守卫2 失败：bot.constants 解析自非本仓库 {c_file}")

    repo_base = _parse_base(os.path.join(real_root, "bot", "local_config.py"))
    prod_base = _parse_base(_PROD_CFG_PATH)
    if not prod_base:
        _fail(f"守卫2 失败：无法从生产配置 {_PROD_CFG_PATH} 解析 BASE_TOKEN，拒绝执行")
    if _constants.BASE_TOKEN == prod_base:
        _fail("守卫2 失败：解析出的 BASE_TOKEN 与生产 base 相同（拒绝连生产）")
    if _constants.BASE_TOKEN != repo_base:
        _fail("守卫2 失败：constants.BASE_TOKEN 与本仓库 bot/local_config.py 文本解析值不一致")
    print("✅ 守卫2 BASE_TOKEN≠生产 且=本仓库 local_config")

    if _constants.USER_TABLE_ID != _EXPECTED_USER_TABLE:
        _fail(f"守卫3 失败：USER_TABLE_ID={_constants.USER_TABLE_ID}≠{_EXPECTED_USER_TABLE}")
    print("✅ 守卫3 USER_TABLE_ID=测试 base 已知值")

    C = _constants
    bitable = _clients.bitable
    from lib.bitable_client import get_field_text as _gft
    from lib.bitable_client import validate_id_card as _vic
    get_field_text, validate_id_card = _gft, _vic


_guard_and_import()


def _load_target(t):
    """读记录并核对身份（record_id 可能被重建/换库，身份不符绝不写入）。"""
    rec = bitable.get_record(C.USER_TABLE_ID, t["rid"])
    if not rec:
        _fail(f"{t['uid']} 读取失败：record_id={t['rid']} 不存在（中止）")
    f = rec.get("fields", {})
    uid = get_field_text(f, _FIELD_UID)
    oid = get_field_text(f, _FIELD_OID)
    nick = get_field_text(f, "昵称")
    if uid != t["uid"] or not oid.startswith(t["oid_prefix"]) or nick != t["nick"]:
        _fail(f"{t['uid']} 身份不符：uid={uid!r} oid={oid[:16]}… nick={nick!r}（中止）")
    return rec, f


def _report():
    all_ok = True
    for t in _TARGETS:
        rec, f = _load_target(t)
        idc = get_field_text(f, _FIELD_ID_CARD).strip()
        ok, msg = validate_id_card(idc)
        all_ok = all_ok and ok
        print(f"  [{t['uid']}] {t['nick']}  身份证现值：{_mask_id(idc, ok)}"
              + ("" if ok else f"  ({msg})"))
    return all_ok


def _cmd_plan():
    print("========== PLAN（只读，不写） ==========")
    _report()
    for t in _TARGETS:
        new_id = _gen_id_card(t["gender"], t["birth"], t["seq"])
        ok, msg = validate_id_card(new_id)
        if not ok:
            _fail(f"生成的身份证号不合法（{t['uid']}）：{msg}")
        print(f"  [{t['uid']}] 拟写入：{_mask_id(new_id, ok)}")
    print("执行修复请跑：run")


def _cmd_run():
    print("========== RUN（只写这两条的身份证号字段） ==========")
    for t in _TARGETS:
        rec, f = _load_target(t)
        idc = get_field_text(f, _FIELD_ID_CARD).strip()
        ok, msg = validate_id_card(idc)
        print(f"  [{t['uid']}] 写前：{_mask_id(idc, ok)}" + ("" if ok else f"  ({msg})"))
        if ok:
            print(f"  [{t['uid']}] 已是合法值，跳过")
            continue
        new_id = _gen_id_card(t["gender"], t["birth"], t["seq"])
        ok_new, msg_new = validate_id_card(new_id)
        if not ok_new:
            _fail(f"生成的身份证号不合法（{t['uid']}）：{msg_new}")
        dup = bitable.search_records(C.USER_TABLE_ID, [
            {"field_name": _FIELD_ID_CARD, "operator": "is", "value": [new_id]}])
        if dup is None:
            _fail(f"{t['uid']} 查重失败（读取返回错误，中止，未写入）")
        others = [r for r in dup if r.get("record_id") != t["rid"]]
        if others:
            _fail(f"{t['uid']} 拟写入号码与其他 {len(others)} 条记录重复（中止）")
        if not bitable.update_record(C.USER_TABLE_ID, t["rid"], {_FIELD_ID_CARD: new_id}):
            _fail(f"{t['uid']} 写入失败（中止）")
        live = bitable.get_record(C.USER_TABLE_ID, t["rid"]) or {}
        back = get_field_text(live.get("fields", {}), _FIELD_ID_CARD).strip()
        ok_back, msg_back = validate_id_card(back)
        if back != new_id or not ok_back:
            _fail(f"{t['uid']} 回读校验失败：{_mask_id(back, ok_back)}（{msg_back}）")
        print(f"  [{t['uid']}] 写后回读：{_mask_id(back, ok_back)}  ✅")
    return _cmd_verify()


def _cmd_verify():
    print("========== VERIFY（只读复核） ==========")
    all_ok = _report()
    if not all_ok:
        _fail("复核未通过：仍有身份证无效的记录")
    print("✅ 复核通过：两条种子身份证均有效。")
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "plan":
        _cmd_plan()
    elif mode == "run":
        return _cmd_run()
    elif mode == "verify":
        return _cmd_verify()
    else:
        print("用法：YIXIANQIAN_ENV=dev bot/venv/bin/python "
              "scripts/dev/fix_test_id_cards.py plan|run|verify")
        sys.exit(2)


if __name__ == "__main__":
    sys.exit(main())
