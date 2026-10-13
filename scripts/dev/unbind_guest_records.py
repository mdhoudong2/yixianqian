#!/usr/bin/env python3
"""解绑「创建人=外部访客」的存量自动绑定记录（一次性数据修复；默认 dry-run，--run 才写）。

背景：微信问卷提交的记录创建人是「访客 XXXX」（外部访客身份）。auto_bind_from_creator
曾把这类记录自动绑定到访客自身（飞书用户ID=创建人id），用户在飞书注册的新身份再来
认领时就匹配不到（认领要求飞书用户ID为空）→ 404。bot 侧已修复为跳过无邀请码的访客
记录（防再犯）；本脚本修复存量：清空这些记录的「飞书用户ID」+ 删除绑定表里
record_id 属于这些记录的条目（lib.storage 加锁原子写）。

清空写法实证（2026-10-13 测试环境一次性探针，先造探针记录、测完即删）：
  先写入一个假 open_id 值，再分别尝试三种清空写法并读回：
    - '""' 空字符串     → update 成功，读回为空【采用】；
    - '[]' 空列表       → 被拒 code=1254060 TextFieldConvFail（该字段为 Multiline
                          文本，"value must be a string"），字段原样未变；
    - 'None'(JSON null) → update 成功，读回为空（亦可用；本脚本采用 ""，语义更明确）。
  故统一以 {FIELD_FEISHU_ID: ""} 清空。

用法（仓库根执行）：
  # 测试环境（dry-run，默认；必须显式 dev 才过 _prod_guard）
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/unbind_guest_records.py
  # 测试环境执行
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/unbind_guest_records.py --run
  # 生产执行（写操作必须显式放行）
  YX_DEV_ALLOW=1 web/backend/venv/bin/python scripts/dev/unbind_guest_records.py --run

守卫：
  1) 仓库根 realpath 必须是 /opt/yixianqian-test 或 /opt/yixianqian（拒绝在别处跑）；
  2) 当前生效的 BASE_TOKEN 必须与运行仓库匹配：运行时正则解析两个仓库的
     bot/local_config.py（不 import），要求生效值=运行仓库解析值、且≠另一仓库
     解析值；任一解析失败即拒绝执行（无法自证）；
  3) 生产环境 --run 必须显式 YX_DEV_ALLOW=1（与 _prod_guard / scripts/dev/README 一致）；
  4) 生产执行前必须确认 bot 已部署「访客记录不自动绑定」修复，否则解绑出的空档会在
     下一个 30 秒轮询里被重新绑定——脚本在 --run 时醒目提示。
"""
import os
import re
import sys

_D = os.path.dirname(os.path.abspath(__file__))
if _D not in sys.path:
    sys.path.insert(0, _D)

from _prod_guard import guard  # noqa: E402

guard(os.path.basename(__file__))

_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(_D)))
_TEST_ROOT = "/opt/yixianqian-test"
_PROD_ROOT = "/opt/yixianqian"
# 两个仓库的本地配置路径：运行时正则解析各自的 BASE_TOKEN（不 import，见 _parse_base）
_TEST_CFG_PATH = os.path.join(_TEST_ROOT, "bot", "local_config.py")
_PROD_CFG_PATH = os.path.join(_PROD_ROOT, "bot", "local_config.py")

if _ROOT == _TEST_ROOT:
    ENV = "test"
elif _ROOT == _PROD_ROOT:
    ENV = "prod"
else:
    print(f"⛔ 守卫1 失败：只能在 {_TEST_ROOT} 或 {_PROD_ROOT} 运行，实际 {_ROOT}")
    sys.exit(1)

for _p in (os.path.join(_ROOT, "bot"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from constants import (  # noqa: E402
    APP_ID,
    APP_SECRET,
    BASE_TOKEN,
    BINDING_FILE,
    FIELD_ACCOUNT_STATUS,
    FIELD_CREATOR,
    FIELD_FEISHU_ID,
    FIELD_NICKNAME,
    USER_TABLE_ID,
)

from lib import storage  # noqa: E402
from lib.bitable_client import BitableClient, get_field_text  # noqa: E402


def _mask(value, keep=8):
    """脱敏显示：只露前缀，避免日志出现完整 token。"""
    value = value or ""
    return value[:keep] + "…" if len(value) > keep else value


def _parse_base(path):
    """从 local_config.py 文本解析 BASE_TOKEN（不 import，避免读到错误环境的配置）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            m = re.search(r'^BASE_TOKEN\s*=\s*"([^"]+)"', fh.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def _verify_base_env(env, eff_base, test_base, prod_base):
    """校验生效 BASE_TOKEN 与运行仓库/环境匹配，返回 (ok, 说明)。

    先要求两个仓库的 bot/local_config.py 都能解析出 BASE_TOKEN（无法自证就不执行）；
    再要求生效值等于本环境仓库的解析值、且不等于另一环境仓库的解析值（防同机两库
    配错/串库；写法同 e2e_claim.py / seed_acceptance.py 的守卫）。
    """
    if not test_base or not prod_base:
        return False, (f"⛔ 守卫2 失败：无法解析仓库 BASE_TOKEN"
                       f"（test={_mask(test_base) or '(解析失败)'} "
                       f"prod={_mask(prod_base) or '(解析失败)'}），无法自证，不执行")
    if env == "test":
        if eff_base != test_base or eff_base == prod_base:
            return False, (f"⛔ 守卫2 失败：生效 BASE_TOKEN（{_mask(eff_base)}）必须=测试仓库值"
                           f"（{_mask(test_base)}）且≠生产仓库值（{_mask(prod_base)}）")
        return True, (f"✅ 守卫2 通过：生效 BASE_TOKEN=测试仓库 local_config 值"
                      f"（{_mask(eff_base)}）且≠生产仓库值")
    if eff_base != prod_base or eff_base == test_base:
        return False, (f"⛔ 守卫2 失败：生效 BASE_TOKEN（{_mask(eff_base)}）必须=生产仓库值"
                       f"（{_mask(prod_base)}）且≠测试仓库值（{_mask(test_base)}）")
    return True, (f"✅ 守卫2 通过：生效 BASE_TOKEN=生产仓库 local_config 值"
                  f"（{_mask(eff_base)}）且≠测试仓库值")


_ok, _msg = _verify_base_env(ENV, BASE_TOKEN, _parse_base(_TEST_CFG_PATH),
                             _parse_base(_PROD_CFG_PATH))
print(_msg)
if not _ok:
    sys.exit(1)

RUN = "--run" in sys.argv
if ENV == "prod" and RUN and os.environ.get("YX_DEV_ALLOW") != "1":
    print("⛔ 守卫3 失败：生产写操作必须显式设置 YX_DEV_ALLOW=1（同 _prod_guard 约定）")
    sys.exit(1)


def _first_creator(fields):
    creators = fields.get(FIELD_CREATOR, [])
    if not creators:
        return {}
    creator = creators[0] if isinstance(creators, list) else creators
    return creator if isinstance(creator, dict) else {}


def fetch_targets(client):
    """全部记录里筛「创建人名以 访客/Guest 开头」且「飞书用户ID==创建人id」的。"""
    records = client.search_records(USER_TABLE_ID) or []
    targets = []
    for rec in records:
        f = rec.get("fields", {})
        creator = _first_creator(f)
        cid = creator.get("id")
        cname = creator.get("name")
        fid = get_field_text(f, FIELD_FEISHU_ID)
        if (fid and cid and fid == cid
                and isinstance(cname, str) and cname.startswith(("访客", "Guest"))):
            targets.append({
                "record_id": rec.get("record_id"),
                "creator_name": cname,
                "status": get_field_text(f, FIELD_ACCOUNT_STATUS),
                "nickname": get_field_text(f, FIELD_NICKNAME),
            })
    return targets


def collect_binding_hits(record_ids):
    """绑定表里 record_id ∈ 目标集合的条目（只读）。"""
    data = storage.load_json(BINDING_FILE, {}) or {}
    return [(k, v) for k, v in data.items()
            if isinstance(v, dict) and v.get("record_id") in record_ids]


def clean_bindings(record_ids):
    """原子删除绑定表里 record_id ∈ 目标集合的条目；返回删除数（无命中零写入）。"""
    removed = [0]

    def _m(data):
        if not isinstance(data, dict):
            data = {}
        stale = [k for k, v in data.items()
                 if isinstance(v, dict) and v.get("record_id") in record_ids]
        if not stale:
            return None
        for k in stale:
            data.pop(k, None)
        removed[0] = len(stale)
        return data

    storage.update_json(BINDING_FILE, {}, _m)
    return removed[0]


def read_feishu_id(client, record_id):
    rec = client.get_record(USER_TABLE_ID, record_id)
    if not rec:
        raise RuntimeError(f"读取记录失败: {record_id}")
    return get_field_text(rec.get("fields", {}), FIELD_FEISHU_ID)


def main():
    client = BitableClient(APP_ID, APP_SECRET, BASE_TOKEN,
                           logger=lambda m: print("  [client]", m))
    mode = "RUN（写数据）" if RUN else "DRY-RUN（只读）"
    print(f"环境: {ENV}（{_ROOT}） base token 前缀: {_mask(BASE_TOKEN)}  模式: {mode}")
    print(f"✅ 守卫1/2 通过：运行仓库与环境配置匹配；绑定表: {BINDING_FILE}")

    targets = fetch_targets(client)
    binding_hits = collect_binding_hits({t["record_id"] for t in targets})

    if not targets:
        print("未发现「创建人=访客 且 飞书用户ID=创建人id」的记录，无需处理"
              f"（绑定表待清理 0 条，共 {len(storage.load_json(BINDING_FILE, {}) or {})} 条）")
        return 0

    print(f"\n将解绑 {len(targets)} 条记录：")
    for t in targets:
        print(f"  - {t['record_id']}  创建人={t['creator_name']}  "
              f"状态={t['status'] or '(空)'}  昵称={t['nickname']}")
    print(f"将清理绑定表条目 {len(binding_hits)} 条")

    if not RUN:
        print("\nDRY-RUN 结束：未写任何数据。确认无误后加 --run 执行。")
        return 0

    if ENV == "prod":
        print("\n⚠️  生产写操作：请先确认 bot 已部署「访客记录不自动绑定」修复，")
        print("    否则解绑出的空档会在下一个 30 秒轮询里被重新绑定。")

    unbound = []
    for t in targets:
        rid = t["record_id"]
        before = read_feishu_id(client, rid)
        if not client.update_record(USER_TABLE_ID, rid, {FIELD_FEISHU_ID: ""}):
            raise RuntimeError(f"清空失败（update 返回失败）: {rid}，已停止")
        after = read_feishu_id(client, rid)
        print(f"解绑 {rid}: 飞书用户ID {before!r} -> {after!r}")
        if after != "":
            raise RuntimeError(f"读回核对失败：{rid} 的飞书用户ID 仍为 {after!r}，已停止")
        unbound.append(rid)

    removed = clean_bindings(set(unbound))
    remain = len(storage.load_json(BINDING_FILE, {}) or {})
    print(f"绑定表清理：删除 {removed} 条 / 剩余 {remain} 条")

    still_bound = [rid for rid in unbound if read_feishu_id(client, rid) != ""]
    if still_bound:
        raise RuntimeError(f"最终复核失败，以下记录飞书用户ID 非空: {still_bound}")
    print(f"\n完成：解绑 {len(unbound)} 条，全部读回为空；绑定条目删除 {removed} 条。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — 数据修复必须显式报错退出，不静默半途
        print(f"⛔ 执行失败，已停止: {e}")
        sys.exit(1)
