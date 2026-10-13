#!/usr/bin/env python3
"""线下一对一「女生坐镇、男生上门」均衡分配离线执行脚本（只读，生产/测试都安全）。

读报名（男女）、读一对一志愿、算资料相似度，解「运输问题」得到「每个女生恰好 K 场、
男生配额均衡（base 或 base+1）」的双向奔赴最优配对，输出两份可打印名单：
  - 男生路线卡：每个男生按得分降序列出要去聊的女生；
  - 女生来访卡：每个女生按得分降序列出会来聊的男生。

不写飞书多维表格（结果只在控制台 + 本地文件），男女/场次全部运行时参数化：
  - 男女人数 = 从活动报名表现场读（报名仍在变，17 号前可能增减）；
  - 女生场次 K = --female-chats 运行时指定。

复用生产同一套读路径：bot/onetoone.configure_onetoone_lib 注入表 ID/栏位名后，
报名读取走 lib.onetoone.build_participants、资料打分走 lib.onetoone.build_profiles，
算法走 lib.onetoone.run_offline_onetoone_matching（与 H5/bot 同一份代码，不会各配各的）。

用法（在仓库根、用 bot venv 执行）：
  bot/venv/bin/python scripts/dev/run_offline_onetoone.py A-0012 --female-chats 15
  bot/venv/bin/python scripts/dev/run_offline_onetoone.py A-0012 --female-chats 15 --out /tmp/名单.txt
"""
import argparse
import os
import re
import sys

_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))

_KNOWN_ROOTS = {
    "/opt/yixianqian": "prod",
    "/opt/yixianqian-test": "test",
}


def _fail(msg):
    print(f"⛔ {msg}")
    sys.exit(1)


def _guard_and_import():
    real_root = os.path.realpath(_ROOT)
    env = _KNOWN_ROOTS.get(real_root)
    if env is None:
        _fail(f"守卫失败：仓库根必须是 {' / '.join(_KNOWN_ROOTS)} 之一，实际 {real_root}")

    for p in (os.path.join(real_root, "bot"), real_root):
        if p not in sys.path:
            sys.path.insert(0, p)

    # 按仓库根锁定 YIXIANQIAN_ENV（test→dev、prod→prod）再 import：constants 靠它
    # 选 ASSISTANT_APP_ID / FEISHU_APP_ID，跑错环境会连错 base 或报 invalid param。
    os.environ["YIXIANQIAN_ENV"] = "dev" if env == "test" else "prod"

    import constants as C
    c_file = os.path.realpath(C.__file__)
    if not c_file.startswith(real_root + os.sep):
        _fail(f"守卫失败：bot.constants 解析自非本仓库 {c_file}")

    import onetoone as bot_onetoone
    from clients import search_records
    from queries import find_activity_by_id, find_user_by_openid

    from lib import onetoone as lib_onetoone
    from lib.bitable_client import get_field_text

    return {
        "env": env,
        "root": real_root,
        "C": C,
        "search_records": search_records,
        "find_activity_by_id": find_activity_by_id,
        "find_user_by_openid": find_user_by_openid,
        "get_field_text": get_field_text,
        "bot_onetoone": bot_onetoone,
        "lib_onetoone": lib_onetoone,
    }


def _num(s):
    m = re.search(r"(\d+)", s or "")
    return int(m.group(1)) if m else 0


def _read_selections(ctx, activity_id):
    C = ctx["C"]
    gft = ctx["get_field_text"]
    selections = {}
    recs = ctx["search_records"](C.ONETOONE_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_OTO_ACTIVITY_ID, "operator": "is",
                        "value": [activity_id]}],
    })
    for sr in recs:
        sf = sr.get("fields", {})
        oid = gft(sf, C.FIELD_OTO_SELECTOR_OID)
        if not oid:
            continue
        choices = []
        for i, cf in enumerate(C.FIELD_OTO_CHOICES):
            val = gft(sf, cf)
            if val:
                choices.append({"id": val, "priority": i + 1})
        if choices:
            selections[oid] = choices
    return selections


def _label(display, oid):
    nick, uid = display.get(oid, ("", ""))
    if nick and uid:
        return f"{nick}（{uid}）"
    if nick:
        return nick
    if uid:
        return uid
    return oid[:10] + "…"


def _sort_oids(oids, display):
    # 按编号数字升序（U-0012 的 12），空/异常兜底 0；同名再按昵称稳定
    return sorted(oids, key=lambda o: (_num(display.get(o, ("", ""))[1]),
                                       display.get(o, ("", ""))[0]))


def main():
    ap = argparse.ArgumentParser(description="线下一对一均衡分配（女生坐镇、男生上门）")
    ap.add_argument("activity_id", help="活动ID，如 A-0012")
    ap.add_argument("--female-chats", type=int, required=True,
                    help="每个女生聊几场（K，运行时指定，报名变化时重跑）")
    ap.add_argument("--out", default=None,
                    help="输出文件路径（默认写到当前目录 offline_<活动ID>.txt）")
    args = ap.parse_args()

    ctx = _guard_and_import()
    C = ctx["C"]
    print(f"✅ 环境 {ctx['env']}（根 {ctx['root']}）")

    # 复用 bot 侧注入：lib.onetoone 的报名读取/资料打分需要表 ID 与栏位名。
    ctx["bot_onetoone"].configure_onetoone_lib(lock_dir="/tmp")

    activity = ctx["find_activity_by_id"](args.activity_id)
    if not activity:
        _fail(f"未找到活动：{args.activity_id}")
    af = activity.get("fields", {})
    activity_id = ctx["get_field_text"](af, C.FIELD_ACTIVITY_ID) or ""
    activity_name = ctx["get_field_text"](af, C.FIELD_ACTIVITY_NAME) or ""
    print(f"活动：{activity_name}（{activity_id}）")

    # 报名 → 参与者 + 展示名（男女/昵称/编号都现场读）
    signups = ctx["bot_onetoone"].get_activity_signups(activity_id)
    participants = []
    display = {}
    males = []
    females = []
    no_gender = []
    for s in signups:
        oid = s["open_id"]
        g = s["gender"]
        if g == "男性":
            participants.append({"id": oid, "gender": "male"})
            males.append(oid)
        elif g == "女性":
            participants.append({"id": oid, "gender": "female"})
            females.append(oid)
        else:
            no_gender.append(s)
            continue
        display[oid] = (s.get("nickname") or "", s.get("user_id") or "")

    M = len(males)
    F = len(females)
    if M == 0 or F == 0:
        _fail(f"报名有效性别不全：男 {M} 人 / 女 {F} 人（无法配对）")

    selections = _read_selections(ctx, activity_id)
    participant_oids = {p["id"] for p in participants}
    submitted_oids = participant_oids & set(selections)
    n_submitted = len(submitted_oids)
    n_unsubmitted = len(participant_oids) - n_submitted

    profiles = ctx["lib_onetoone"].build_profiles(
        [p["id"] for p in participants],
        find_user_by_openid=ctx["find_user_by_openid"])

    try:
        result = ctx["lib_onetoone"].run_offline_onetoone_matching(
            participants, selections, profiles, args.female_chats)
    except ValueError as e:
        _fail(str(e))

    K = args.female_chats
    E = F * K
    base, rem = divmod(E, M)

    header_lines = [
        f"活动「{activity_name}」（{activity_id}）线下一对一名单",
        f"男 {M} 人 · 女 {F} 人 · 女生每人 {K} 场 · "
        f"男生每人 {base}~{base + (1 if rem else 0)} 场 · 共 {E} 场",
        f"已提交志愿 {n_submitted}/{len(participant_oids)} 人"
        + (f"（未提交 {n_unsubmitted} 人，按匹配度 + 对方意愿参与）" if n_unsubmitted else ""),
        "",
    ]
    if no_gender:
        header_lines.append(f"⚠️ {len(no_gender)} 人报名但性别缺失，已排除："
                            + "、".join(s.get("nickname") or s.get("open_id", "")[:8]
                                        for s in no_gender))
        header_lines.append("")

    male_lines = ["═══════════ 男生路线卡（按得分从高到低，去找这些女生聊）═══════════"]
    for oid in _sort_oids(males, display):
        lst = result["male_lists"].get(oid, [])
        male_lines.append(f"【{_label(display, oid)}】共 {len(lst)} 场")
        for fid, rank, _sc in lst:
            male_lines.append(f"    {rank}. {_label(display, fid)}")

    female_lines = ["═══════════ 女生来访卡（按得分从高到低，这些男生会来找你）═══════════"]
    for oid in _sort_oids(females, display):
        lst = result["female_lists"].get(oid, [])
        female_lines.append(f"【{_label(display, oid)}】共 {len(lst)} 场")
        for mid, _sc in lst:
            female_lines.append(f"    {_label(display, mid)}")

    output = "\n".join(header_lines + male_lines + [""] + female_lines)
    print(output)

    out_path = args.out or os.path.join(os.getcwd(), f"offline_{activity_id}.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(output + "\n")
    print(f"\n✅ 已写入 {out_path}")


if __name__ == "__main__":
    main()
