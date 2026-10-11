"""活动一对一：双向奔赴匹配算法 + 志愿收集/执行（bot 与 H5 后端共用）。

与 lib/grouping.py 同构：

- 本模块**不得** import bot/ 或 web/backend/ 下的任何模块（constants / config /
  clients / queries）：表 ID、栏位名、锁目录一律由两端启动时各自 `configure()` 注入。
  写错 base 时不会有任何异常，只会把一对一结果写进另一张表——少传就在用到时报
  RuntimeError，绝不猜默认值。
- 对外一律回传 dict（含 `code` 栏位）、不回传字串：文案归渲染层。
- 一次性写路径（执行匹配）刻意用 raw client：`None`（读失败）≠ `[]`（空表）。
  把读失败当空表会重跑已有结果或在回滚时谎称「已回滚」，见 tests/test_onetoone_idem.py。

`run_onetoone_matching` 的权重常量已随算法从 bot/constants.py 搬进本模块：
两端永远相同，不是环境配置。
"""
from concurrent.futures import ThreadPoolExecutor

from lib import grouping as lib_grouping
from lib import recommend
from lib.bitable_client import get_field_text, get_multi_select_value
from lib.grouping import PRIORITY_SCORES, GroupingBusy, grouping_lock
from lib.util import log

# ---------------------------------------------------------------- 运行配置

_REQUIRED_TABLES = ("activity_table_id", "signup_table_id",
                    "onetoone_select_table", "onetoone_result_table")

# 栏位名用语义键（不是 bot 的 FIELD_xxx、也不是 web 的 F_XXX）：lib 不该知道
# 任何一端的命名习惯，两端各自把自己的常量翻译过来。
_REQUIRED_FIELDS = (
    "activity_id", "activity_name", "onetoone_status", "onetoone_flag",
    "group_flag", "group_status",
    "oto_activity_id", "oto_selector_oid", "oto_selector_name", "oto_selector_gender",
    "oto_choices", "oto_rank", "oto_user_oid", "oto_user_name", "oto_user_gender",
    "oto_target_oid", "oto_target_name",
    "signup_activity_id", "signup_openid", "signup_nickname", "signup_status",
    "gender", "user_id",
    "self_hobbies", "self_sports", "self_traits", "education", "mbti", "city", "church",
)
_cfg = {"tables": {}, "fields": {}}


def configure(*, onetoone_select_table=None, onetoone_result_table=None,
              activity_table_id=None, signup_table_id=None, fields=None, lock_dir=None):
    """注入表 ID、栏位名与锁目录。两端启动时各调一次。

    lock_dir 会一并转注入 lib.grouping：一对一提交/开始的跨进程互斥复用分组那批
    `yixianqian_grouping_<活动>.lock`（bot 与 H5 共享同一把活动锁）。只用纯算法或
    纯读的调用方可以不传。
    """
    unknown = [k for k in (fields or {}) if k not in _REQUIRED_FIELDS]
    if unknown:
        raise RuntimeError(f"lib.onetoone.configure 收到未知栏位名：{unknown}")
    if fields:
        merged = {**_cfg["fields"], **fields}
        missing = [k for k in _REQUIRED_FIELDS if k not in merged]
        if missing:
            raise RuntimeError(f"lib.onetoone.configure 缺少栏位名：{missing}")
        _cfg["fields"] = merged
    for key, value in (("activity_table_id", activity_table_id),
                       ("signup_table_id", signup_table_id),
                       ("onetoone_select_table", onetoone_select_table),
                       ("onetoone_result_table", onetoone_result_table)):
        if value is not None:
            _cfg["tables"][key] = value
    if lock_dir is not None:
        from lib import grouping as lib_grouping
        lib_grouping.configure(lock_dir=lock_dir)


def _require_configured():
    """业务入口的配置闸门：表 ID 与栏位名必须齐全，缺哪几项直接点名。"""
    missing = [k for k in _REQUIRED_TABLES if not _cfg["tables"].get(k)]
    missing_fields = [k for k in _REQUIRED_FIELDS if k not in _cfg["fields"]]
    if missing or missing_fields:
        detail = []
        if missing:
            detail.append(f"缺少表 ID：{missing}")
        if missing_fields:
            detail.append(f"缺少栏位名：{missing_fields}")
        raise RuntimeError(
            "lib.onetoone 未 configure 完整，不能执行一对一业务（"
            + "；".join(detail)
            + "）。表 ID 与栏位名必须由调用端注入——沉默的默认值等于写进错误的 base。")


# 一对一匹配（双向奔赴模型）常量：总分 = 我的优先级得分×0.5 + 对方的优先级得分×0.3
# + 资料相似度×0.2。「未选择TA」给 30 分基础分，让「选了我但我没主动选」的人也有机会进名单。
WEIGHT_ONETOONE_MY_PRIORITY = 0.5
WEIGHT_ONETOONE_THEIR_PRIORITY = 0.3
WEIGHT_ONETOONE_MATCH = 0.2
ONETOONE_UNSELECTED_SCORE = 30

# 一对一执行互斥已改走 lib.grouping.grouping_lock（跨进程 flock，bot 与 H5 共用
# 同一批 yixianqian_grouping_<活动>.lock）。等待预算：执行/开始收集是分钟级作业，
# 与分组同口径；用户提交要快速失败，不占线程池。
START_LOCK_TIMEOUT = 180
SUBMIT_LOCK_TIMEOUT = 5


# ---------------------------------------------------------------- 匹配算法

def run_onetoone_matching(participants, selections, profiles, top_n=10):
    """双向奔赴 + 资料相似度：给每人算出 up to top_n 个异性必聊名单（各自独立生成）。

    participants: [{"id": openid, "gender": "male"/"female"}, ...]
    selections: {selector_oid: [{"id": target_oid, "priority": 1..7}, ...]}
    profiles: {oid: 打分资料dict}（键与 lib.recommend.match_score 对齐，已 prepare_profile）
    返回: {oid: [(target_oid, rank), ...]}，rank 从 1 开始。

    总分 = 我的优先级得分 × WEIGHT_ONETOONE_MY_PRIORITY
         + 对方的优先级得分 × WEIGHT_ONETOONE_THEIR_PRIORITY
         + 资料相似度 × WEIGHT_ONETOONE_MATCH
    「优先级得分」= PRIORITY_SCORES[志愿位次]；未选择则 ONETOONE_UNSELECTED_SCORE(30)。
    「我的优先级得分」看我选没选 TA；「对方的优先级得分」看 TA 选没选我——所以
    没被我主动选、但对我示好的人也有机会进名单（惊喜位）。
    """
    males = [p["id"] for p in participants if p.get("gender") == "male"]
    females = [p["id"] for p in participants if p.get("gender") == "female"]

    # 反向索引：target_oid -> {selector_oid: priority}，用来算「对方对我的优先级得分」。
    chosen_by = {}
    for sel_oid, choices in selections.items():
        for c in choices:
            chosen_by.setdefault(c["id"], {})[sel_oid] = c["priority"]

    result = {}
    for p in participants:
        pid = p["id"]
        g = p.get("gender")
        if g == "male":
            opp = females
        elif g == "female":
            opp = males
        else:
            # 性别缺失/未知：不进任何人的候选，也不给名单
            result[pid] = []
            continue
        candidates = [q for q in opp if q != pid]

        # 我选的人：target -> priority（同人多次填取最小优先级，即最高意愿）
        my_choice = {}
        for s in selections.get(pid, []):
            if s["id"] not in my_choice or s["priority"] < my_choice[s["id"]]:
                my_choice[s["id"]] = s["priority"]

        prof_a = profiles.get(pid, {})
        scored = []
        for qid in candidates:
            my_p = my_choice.get(qid)
            my_score = PRIORITY_SCORES.get(my_p, ONETOONE_UNSELECTED_SCORE)
            # 对方对我的优先级得分 = 「qid 有没有选我」→ chosen_by[target=pid][selector=qid]
            their_p = chosen_by.get(pid, {}).get(qid)
            their_score = PRIORITY_SCORES.get(their_p, ONETOONE_UNSELECTED_SCORE)
            try:
                match = recommend.match_score(prof_a, profiles.get(qid, {}))[0]
            except Exception:
                match = 0
            total = (WEIGHT_ONETOONE_MY_PRIORITY * my_score
                     + WEIGHT_ONETOONE_THEIR_PRIORITY * their_score
                     + WEIGHT_ONETOONE_MATCH * match)
            scored.append((total, qid))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:min(top_n, len(scored))]
        result[pid] = [(qid, rank) for rank, (_, qid) in enumerate(top, 1)]
    return result


def profile_of(fields, field_names=None):
    """用户表一行 → 打分用的资料 dict（键与 lib.recommend.match_score 对齐）。

    纯函式。field_names 不传时用 configure 注入的栏位名；bot/auto_tasks.py 的
    _profile_of 显式传入自己的常量（周推荐任务不依赖 configure 是否就绪），
    两边读取的栏位集合必须一致。
    """
    names = field_names
    if names is None:
        missing = [k for k in ("self_hobbies", "self_sports", "self_traits", "education",
                               "mbti", "city", "church") if k not in _cfg["fields"]]
        if missing:
            raise RuntimeError(f"lib.onetoone 未 configure 打分栏位名：{missing}")
        names = _cfg["fields"]
    return recommend.prepare_profile({
        "hobbies": get_multi_select_value(fields, names["self_hobbies"]),
        "sports": get_multi_select_value(fields, names["self_sports"]),
        "traits": get_multi_select_value(fields, names["self_traits"]),
        "education": get_field_text(fields, names["education"]),
        "mbti": get_multi_select_value(fields, names["mbti"]),
        "city": get_field_text(fields, names["city"]),
        "church": get_field_text(fields, names["church"]),
    })


def build_profiles(open_ids, *, find_user_by_openid):
    """为每个 open_id 构建打分资料（键与 lib.recommend.match_score 对齐）。"""
    def _fetch(oid):
        recs = find_user_by_openid(oid)
        if not recs:
            return oid, {}
        return oid, profile_of(recs[0].get("fields", {}))

    profiles = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="onetoone-profile") as pool:
        for oid, prof in pool.map(_fetch, open_ids):
            profiles[oid] = prof
    return profiles


# ---------------------------------------------------------------- 内部读取

def build_participants(activity_id, *, client, find_user_by_openid):
    """取该活动的报名者：[{open_id, nickname, gender, user_id}]。

    读取失败（raw client 返回 None）按空表处理：这些数据只用于名单展示/候选校验，
    飞书抖一下不该让整条指令失败——与迁移前 bot 经 lib.grouping.collect_participants
    的兜底口径一致。性别/用户ID 走调用方注入的 find_user_by_openid。
    """
    _require_configured()
    f = _cfg["fields"]
    signups = client.search_records(_cfg["tables"]["signup_table_id"], {
        "conjunction": "and",
        "conditions": [
            {"field_name": f["signup_activity_id"], "operator": "is", "value": [activity_id]},
            {"field_name": f["signup_status"], "operator": "is", "value": ["已报名"]},
        ]}) or []

    signup_rows = []
    for s in signups:
        sf = s.get("fields", {})
        oid = get_field_text(sf, f["signup_openid"])
        nickname = get_field_text(sf, f["signup_nickname"])
        if oid:
            signup_rows.append((oid, nickname))

    def _fetch(row):
        oid, nickname = row
        gender = ""
        uid = ""
        user_recs = find_user_by_openid(oid)
        if user_recs:
            uf = user_recs[0].get("fields", {})
            gender = get_field_text(uf, f["gender"])
            uid = get_field_text(uf, f["user_id"])
        return {"open_id": oid, "nickname": nickname, "gender": gender, "user_id": uid}

    users = []
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="onetoone-signup") as pool:
        for u in pool.map(_fetch, signup_rows):
            users.append(u)
    return users


def fetch_selection(activity_id, open_id, *, client):
    """某人该活动已提交的一对一志愿：{"record_id", "choices"} 或 None。

    只用于卡片回显默认值/旧入口兼容；读失败按「没有」处理（与旧 bot 经
    search_records(None→[]) 的口径一致）。
    """
    _require_configured()
    f = _cfg["fields"]
    records = client.search_records(_cfg["tables"]["onetoone_select_table"], {
        "conjunction": "and",
        "conditions": [
            {"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]},
            {"field_name": f["oto_selector_oid"], "operator": "is", "value": [open_id]},
        ]}) or []
    if not records:
        return None
    fields = records[0].get("fields", {})
    choices = []
    for cf in f["oto_choices"]:
        val = get_field_text(fields, cf)
        if val:
            choices.append(val)
    return {"record_id": records[0]["record_id"], "choices": choices}


def _scan_selections(records):
    """扫描志愿记录 → (participants, selections, skipped)。

    与迁移前 bot/_do_onetoone 逐行等价：性别不是「男性/女性」的跳过（不计入
    去重位，首行性别缺失、次行有效时人仍参与——刻意保留的既有语义）。
    """
    f = _cfg["fields"]
    participants = []
    seen_oids = set()
    selections = {}
    skipped = 0
    for sr in records:
        sf = sr.get("fields", {})
        oid = get_field_text(sf, f["oto_selector_oid"])
        gender = get_field_text(sf, f["oto_selector_gender"])
        if not oid or oid in seen_oids:
            continue
        if gender not in ("男性", "女性"):
            skipped += 1
            continue
        seen_oids.add(oid)
        participants.append({"id": oid, "gender": "male" if gender == "男性" else "female"})
        choices = []
        for i, cf in enumerate(f["oto_choices"]):
            val = get_field_text(sf, cf)
            if val:
                choices.append({"id": val, "priority": i + 1})
        if choices:
            selections[oid] = choices
    return participants, selections, skipped


# ---------------------------------------------------------------- 读接口

def fetch_status(activity_id, *, client, activity, find_user_by_openid):
    """一对一状态：管理台要的那几个数字。"""
    _require_configured()
    f = _cfg["fields"]
    af = activity.get("fields", {})
    name = get_field_text(af, f["activity_name"])
    status = get_field_text(af, f["onetoone_status"]) or "未开始"
    signups = build_participants(activity_id, client=client, find_user_by_openid=find_user_by_openid)
    selections = client.search_records(_cfg["tables"]["onetoone_select_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]}],
    }) or []
    # 去重口径与分组端统一：非空 open_id 去重（同一人重复行只算一次）
    submitted_oids = {
        get_field_text(s.get("fields", {}), f["oto_selector_oid"]) for s in selections
        if get_field_text(s.get("fields", {}), f["oto_selector_oid"])
    }
    return {
        "code": "OK",
        "activity_id": activity_id,
        "name": name,
        "status": status,
        "signup_count": len(signups),
        "submitted_count": len(submitted_oids),
    }


def fetch_unsubmitted(activity_id, *, client, activity, find_user_by_openid):
    """未提交一对一志愿的报名者名单。"""
    _require_configured()
    f = _cfg["fields"]
    act_name = get_field_text(activity.get("fields", {}), f["activity_name"])
    signups = build_participants(activity_id, client=client, find_user_by_openid=find_user_by_openid)
    selections = client.search_records(_cfg["tables"]["onetoone_select_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]}],
    }) or []
    submitted_oids = {
        get_field_text(s.get("fields", {}), f["oto_selector_oid"])
        for s in selections if get_field_text(s.get("fields", {}), f["oto_selector_oid"])
    }

    unsubmitted = []
    for s in signups:
        oid = s["open_id"]
        if oid in submitted_oids:
            continue
        user_id = ""
        for rec in find_user_by_openid(oid):
            user_id = get_field_text(rec.get("fields", {}), f["user_id"])
        unsubmitted.append({
            "open_id": oid, "user_id": user_id,
            "gender": s["gender"], "nickname": s["nickname"],
        })

    return {
        "code": "OK",
        "activity_id": activity_id,
        "name": act_name,
        "total": len(unsubmitted),
        "unsubmitted": unsubmitted,
        "all_submitted": not unsubmitted,
    }


def preview(activity_id, *, client, activity, find_user_by_openid):
    """执行前的影响面预览——**一个栏位都不写**（对照 lib.grouping.preview_round）。

    bot 端暂未接线；H5 管理台后续要预览时直接复用同一口径。
    """
    _require_configured()
    f = _cfg["fields"]
    af = activity.get("fields", {})
    name = get_field_text(af, f["activity_name"])
    status = get_field_text(af, f["onetoone_status"]) or "未开始"
    rec_activity_id = get_field_text(af, f["activity_id"]) or ""

    rows = client.search_records(_cfg["tables"]["onetoone_select_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [rec_activity_id]}],
    }) or []
    participants, _selections, skipped = _scan_selections(rows)

    existing = client.search_records(_cfg["tables"]["onetoone_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [rec_activity_id]}],
    }) or []

    blockers = []
    if not participants:
        blockers.append("没有已提交志愿的参与者")
    if existing:
        blockers.append(f"已生成过一对一结果（{len(existing)} 条），如需重跑请先发送「开始一对一」清理")

    return {
        "code": "OK",
        "activity_id": activity_id,
        "name": name,
        "onetoone_status": status,
        "participant_count": len(participants),
        "skipped_no_gender": skipped,
        "existing_results": len(existing),
        "can_execute": not blockers,
        "blockers": blockers,
    }


# ---------------------------------------------------------------- 写接口

def validate_choices(choices, participants, user_gender):
    """校验一对一志愿列表（纯函数）。

    统一为 **1..7**（与 H5 端点同一口径；小活动凑不满 7 人时也能提交），并校验
    无重复、每个目标都∈「异性＋本活动已报名」候选集。
    返回 {"code": "OK"} 或 {"code": "COUNT"/"DUPLICATE"/"INVALID_TARGET", ...}。
    """
    if not isinstance(choices, list) or not all(isinstance(c, str) and c for c in choices):
        return {"code": "COUNT"}
    if not 1 <= len(choices) <= 7:
        return {"code": "COUNT"}
    if len(set(choices)) != len(choices):
        return {"code": "DUPLICATE"}
    opp_gender = "女性" if user_gender == "男性" else "男性"
    valid_oids = {p["open_id"] for p in participants if p["gender"] == opp_gender}
    for i, c in enumerate(choices):
        if c not in valid_oids:
            return {"code": "INVALID_TARGET", "index": i}
    return {"code": "OK"}


def save_selection(activity_id, open_id, fields, *, client, lock_timeout=None):
    """写入/更新某人的一对一志愿（**锁内**查重后 create 或 update）。

    调用方不需要持锁；本函数内部持 lib.grouping.grouping_lock(activity_id)。
    三点刻意语义：
    - 查重必须与写入同处锁内：锁外双查双空的双击请求会各插一行（与分组端
      「审查必修 7」同一修法）；
    - 用 raw client：读失败（None）直接中止，绝不当空表去 create——那是重复志愿
      行的源头；快照/实时两条读取路径由调用端负责（bot 只有实时一条）；
    - 写失败如实返回，调用方不得报成功。
    返回 {"code": "OK", "updated": bool}；BUSY / SEARCH_FAILED / WRITE_FAILED。
    """
    if lock_timeout is None:
        lock_timeout = SUBMIT_LOCK_TIMEOUT
    _require_configured()
    f = _cfg["fields"]
    t = _cfg["tables"]
    try:
        with grouping_lock(activity_id, timeout=lock_timeout):
            existing = client.search_records(t["onetoone_select_table"], {
                "conjunction": "and",
                "conditions": [
                    {"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]},
                    {"field_name": f["oto_selector_oid"], "operator": "is", "value": [open_id]},
                ]})
            if existing is None:
                return {"code": "SEARCH_FAILED"}
            if existing:
                if not client.update_record(t["onetoone_select_table"],
                                            existing[0]["record_id"], fields):
                    return {"code": "WRITE_FAILED"}
                return {"code": "OK", "updated": True}
            if not client.create_record(t["onetoone_select_table"], fields):
                return {"code": "WRITE_FAILED"}
            return {"code": "OK", "updated": False}
    except GroupingBusy:
        return {"code": "BUSY"}


def start_collection(activity_id, *, client, activity, lock_timeout=None):
    """开放一对一志愿收集；**整体持锁**切状态 + 清空该活动全部历史结果。

    锁内做「状态→收集中 + 清历史结果」：不持锁时，并发的「开始一对一」与
    「执行一对一」会互相删除/覆盖结果。删除数不足必须明报失败——报成功会让
    管理员以为可以重跑，实际被残留结果卡住。

    动作层互斥（设计 §2.3）：对面分组 flag=「是」或状态 ∈ {收集中, 已截止} →
    拒绝（EXCLUSIVE_MODE），不自动动对面数据。假表缺分组栏位 → 不触发。

    返回 code：OK / BUSY / EXCLUSIVE_MODE / STATUS_FAILED / RESULTS_SEARCH_FAILED /
    DELETE_INCOMPLETE。
    """
    if lock_timeout is None:
        lock_timeout = START_LOCK_TIMEOUT
    _require_configured()
    f = _cfg["fields"]
    t = _cfg["tables"]
    af = activity.get("fields", {})
    activity_name = get_field_text(af, f["activity_name"])
    record_id = activity.get("record_id")
    base = {"activity_id": activity_id, "activity_name": activity_name}
    if lib_grouping.counterpart_start_active(activity, flag_field=f["group_flag"],
                                             status_field=f["group_status"]):
        return dict(base, code="EXCLUSIVE_MODE", blocked_by="group")
    try:
        with grouping_lock(activity_id, timeout=lock_timeout):
            if not client.update_record(t["activity_table_id"], record_id,
                                        {f["onetoone_status"]: "收集中"}):
                return dict(base, code="STATUS_FAILED")

            old_results = client.search_records(t["onetoone_result_table"], {
                "conjunction": "and",
                "conditions": [{"field_name": f["oto_activity_id"], "operator": "is",
                                "value": [activity_id]}],
            })
            if old_results is None:
                return dict(base, code="RESULTS_SEARCH_FAILED")
            deleted = 0
            if old_results:
                deleted = client.batch_delete_records(
                    t["onetoone_result_table"], [r["record_id"] for r in old_results])
                if deleted < len(old_results):
                    return dict(base, code="DELETE_INCOMPLETE",
                                deleted=deleted, total=len(old_results))
                log(f"重新开放一对一，已清除 {deleted} 条历史结果: {activity_name}")

            log(f"管理员开始一对一: {activity_name}")
            return dict(base, code="OK", cleared_results=deleted)
    except GroupingBusy:
        return dict(base, code="BUSY")


def execute(activity_id, *, client, activity, find_user_by_openid, lock_timeout=None):
    """执行一对一匹配（幂等检查 + 状态机 + 写结果）。

    **调用方不需要持锁**：本函数整体持 lib.grouping.grouping_lock（跨进程 flock，
    与 H5/分组共用同一批活动锁）。状态机每一步的读失败（raw client 返回 None）
    都在写入之前中止，绝不当空表继续：

    ① 幂等查历史结果，None → 中止（零写入）；
    ② 写「已截止」，失败 → 中止；
    ③ 读志愿，None → 恢复「收集中」（并检查恢复结果）＋明说失败；
    ④ 空 → 维持原「没有参与者」文案；⑤ 算法异常 → 恢复状态；
    ⑥ 写结果，`created < total` → 回查：None → 不得宣称已回滚；回查成功 → 删除
       并检查删除数；
    ⑦ 成功 → 状态「已完成」（检查返回值）。

    返回 code：OK / BUSY / SEARCH_FAILED / ALREADY_DONE / CLOSE_FAILED /
    SELECTIONS_SEARCH_FAILED / NO_PARTICIPANTS / ALGORITHM_ERROR /
    PARTIAL_WRITE / PARTIAL_WRITE_VERIFY_FAILED / PARTIAL_WRITE_INCOMPLETE /
    DONE_STATUS_FAILED。
    """
    if lock_timeout is None:
        lock_timeout = START_LOCK_TIMEOUT
    _require_configured()
    f = _cfg["fields"]
    activity_name = get_field_text(activity.get("fields", {}), f["activity_name"])
    base = {"activity_id": activity_id, "activity_name": activity_name}
    # 动作层互斥：执行一对一前对面分组 flag=「是」→ 拒绝（不自动动对面数据）。
    # 假表缺分组栏位 → 不触发；锁前判定，命中时零写入零等待。
    if lib_grouping.counterpart_execute_active(activity, flag_field=f["group_flag"]):
        return dict(base, code="EXCLUSIVE_MODE", blocked_by="group")
    try:
        with grouping_lock(activity_id, timeout=lock_timeout):
            return _execute_locked(activity_id, client=client, activity=activity,
                                   find_user_by_openid=find_user_by_openid)
    except GroupingBusy:
        return dict(base, code="BUSY")


def _execute_locked(activity_id, *, client, activity, find_user_by_openid):
    _require_configured()
    f = _cfg["fields"]
    t = _cfg["tables"]
    af = activity.get("fields", {})
    activity_name = get_field_text(af, f["activity_name"])
    record_id = activity.get("record_id")
    base = {"activity_id": activity_id, "activity_name": activity_name}

    # ① 幂等保护：该活动已生成过结果则拒绝重跑。读失败（None）中止、零写入——
    # 把「读不到」当「没跑过」去重跑，等于用一次限流换掉一份既有结果。
    existing_results = client.search_records(t["onetoone_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]}],
    })
    if existing_results is None:
        return dict(base, code="SEARCH_FAILED")
    if existing_results:
        return dict(base, code="ALREADY_DONE")

    # 后续所有读写都用活动记录自己的 ID，不退回入参：入参可能是用户随手敲的别名
    rec_activity_id = get_field_text(af, f["activity_id"]) or ""

    # ② 状态切「已截止」：写失败就中止，不能带着「收集中」继续写结果
    if not client.update_record(t["activity_table_id"], record_id,
                                {f["onetoone_status"]: "已截止"}):
        return dict(base, code="CLOSE_FAILED")

    # ③ 读志愿记录：读失败恢复「收集中」并如实报告
    selections_records = client.search_records(t["onetoone_select_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is",
                        "value": [rec_activity_id]}],
    })
    if selections_records is None:
        restored = bool(client.update_record(t["activity_table_id"], record_id,
                                             {f["onetoone_status"]: "收集中"}))
        return dict(base, code="SELECTIONS_SEARCH_FAILED", restored=restored)
    participants, selections, skipped = _scan_selections(selections_records)

    # ③.5 现场签到执行池过滤（设计 §3.3/§3.4，与 lib.grouping.execute_round 同一
    # 兜底）：只挡提交挡不住「早提交、活动当天未到」；读失败（None）fail-closed，
    # 恢复「收集中」再明报，绝不带着「读不到」把全部人放进来。
    if lib_grouping.signin_enabled(af):
        attendance = lib_grouping.attendance_map(rec_activity_id, client=client)
        if attendance is None:
            restored = bool(client.update_record(t["activity_table_id"], record_id,
                                                 {f["onetoone_status"]: "收集中"}))
            return dict(base, code="SIGNIN_QUERY_FAILED", restored=restored)
        participants = [p for p in participants
                        if lib_grouping.is_checked_in(attendance.get(p["id"]))]

    # ④ 没有参与者：恢复状态，维持原文案
    if not participants:
        restored = bool(client.update_record(t["activity_table_id"], record_id,
                                             {f["onetoone_status"]: "收集中"}))
        return dict(base, code="NO_PARTICIPANTS", skipped=skipped, restored=restored)

    profiles = build_profiles([p["id"] for p in participants],
                              find_user_by_openid=find_user_by_openid)

    # ⑤ 算法异常：恢复状态
    try:
        matches = run_onetoone_matching(participants, selections, profiles, top_n=10)
    except Exception as e:
        restored = bool(client.update_record(t["activity_table_id"], record_id,
                                             {f["onetoone_status"]: "收集中"}))
        return dict(base, code="ALGORITHM_ERROR", error=str(e), restored=restored)

    # 报名信息：open_id -> 昵称
    oid_to_nickname = {p["open_id"]: p["nickname"] for p in build_participants(
        rec_activity_id, client=client, find_user_by_openid=find_user_by_openid)}
    result_records = []
    for p in participants:
        pid = p["id"]
        for target_oid, rank in matches.get(pid, []):
            result_records.append({
                f["oto_activity_id"]: rec_activity_id,
                f["oto_rank"]: rank,
                f["oto_user_oid"]: pid,
                f["oto_user_name"]: oid_to_nickname.get(pid, ""),
                f["oto_user_gender"]: "男性" if p["gender"] == "male" else "女性",
                f["oto_target_oid"]: target_oid,
                f["oto_target_name"]: oid_to_nickname.get(target_oid, ""),
            })

    if result_records:
        created = client.batch_create_records(t["onetoone_result_table"], result_records)
        if created < len(result_records):
            # ⑥ 写入不完整：回查已写入的部分结果并回滚。
            # 回查失败（None）时**不得宣称已回滚**——可能残留在表里，谎报成功
            # 会把管理员的后续重试引向「结果重复」或「被幂等卡死」。
            partial = client.search_records(t["onetoone_result_table"], {
                "conjunction": "and",
                "conditions": [{"field_name": f["oto_activity_id"], "operator": "is",
                                "value": [rec_activity_id]}],
            })
            if partial is None:
                restored = bool(client.update_record(t["activity_table_id"], record_id,
                                                     {f["onetoone_status"]: "收集中"}))
                return dict(base, code="PARTIAL_WRITE_VERIFY_FAILED", created=created,
                            total=len(result_records), restored=restored)
            deleted = 0
            if partial:
                deleted = client.batch_delete_records(
                    t["onetoone_result_table"], [p["record_id"] for p in partial])
            if deleted < len(partial):
                restored = bool(client.update_record(t["activity_table_id"], record_id,
                                                     {f["onetoone_status"]: "收集中"}))
                return dict(base, code="PARTIAL_WRITE_INCOMPLETE", created=created,
                            total=len(result_records), deleted=deleted,
                            residual=len(partial) - deleted, restored=restored)
            restored = bool(client.update_record(t["activity_table_id"], record_id,
                                                 {f["onetoone_status"]: "收集中"}))
            return dict(base, code="PARTIAL_WRITE", created=created,
                        total=len(result_records), restored=restored)

    # ⑦ 成功 → 状态「已完成」（检查返回值：结果已写但状态写失败必须明说）
    total_pairs = sum(len(v) for v in matches.values())
    if not client.update_record(t["activity_table_id"], record_id,
                                {f["onetoone_status"]: "已完成"}):
        return dict(base, code="DONE_STATUS_FAILED", participant_count=len(participants),
                    total_pairs=total_pairs)

    log(f"一对一匹配完成: {activity_name}, {len(participants)}人, {total_pairs}条必聊")
    return dict(base, code="OK", participant_count=len(participants), total_pairs=total_pairs)


def _resolve_activity(activity_id, activity, client):
    """activity 缺省时按 activity_id 查一次活动表。返回 (activity, error_code)。"""
    if activity is not None:
        return activity, None
    f = _cfg["fields"]
    records = client.search_records(_cfg["tables"]["activity_table_id"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["activity_id"], "operator": "is",
                        "value": [activity_id]}]})
    if records is None:
        return None, "SEARCH_FAILED"
    if not records:
        return None, "NOT_FOUND"
    return records[0], None


def set_onetoone_flag(activity_id, enabled, *, client, activity=None):
    """开启/关闭活动的「一对一功能开启」开关（控制 H5「我的一对一」入口）。

    - 开启：走 lib.grouping.set_mode(activity_id, "onetoone")——整体持活动锁后一次
      PUT 同时写「一对一=是 + 分组=否」两个栏位（互斥写入器），与 bot
      「开启一对一功能」、H5 模式切换同一套语义；
    - 关闭：只写自身「一对一功能开启=否」（设计 §2.2：关闭时只写自身=否；不碰
      分组栏位，也不在「双是」的历史脏数据上做隐式纠偏——那由控制台一键纠偏）。
      该独立写同样整段持 grouping_lock。

    **不可重入**：开启路已委托给带锁的 set_mode，本函数外层不得再持同一把锁——
    否则嵌套死锁。两种路径的写都与分组/模式切换互相串行化：飞书单条记录 update
    不是整笔事务，并发双栏位 PUT 会栏位级交错留下「双是」（见
    lib.grouping.FLAG_LOCK_TIMEOUT 与 docs/读取一致性.md）。抢不到锁（执行作业
    分钟级持锁）快速失败回 BUSY。

    `activity` 由调用端在锁外解析好（与 start_group 等同约定：网络读不压在锁里），
    产品调用端一律显式传入：真实 base 的「活动ID」是自动编号字段，按字符串过滤
    必回 0 行。`activity=None` 时在锁内查一次——该路径仅供桩表/测试使用、真表
    不可用（只会得到 NOT_FOUND），不是产品路径。写失败如实回 WRITE_FAILED
    （检查 update_record 返回值，不报成功）。
    返回 {"code", "activity_id", "name", "onetoone_flag", "enabled"}。
    """
    _require_configured()
    f = _cfg["fields"]
    if enabled:
        res = lib_grouping.set_mode(activity_id, "onetoone", client=client, activity=activity)
        return {
            "code": res["code"],
            "activity_id": res.get("activity_id", activity_id),
            "name": res.get("name", ""),
            "onetoone_flag": res.get("onetoone_flag", "是") if res["code"] == "OK" else "",
            "enabled": True,
        }

    # 关闭：单栏位独立写，整段持活动锁。锁不可重入，这里不能再调已带锁的 set_mode。
    name_hint = get_field_text((activity or {}).get("fields", {}), f["activity_name"])
    try:
        with grouping_lock(activity_id, timeout=lib_grouping.FLAG_LOCK_TIMEOUT):
            act, error = _resolve_activity(activity_id, activity, client)
            if error:
                return {"code": error, "activity_id": activity_id, "name": name_hint,
                        "onetoone_flag": "", "enabled": False}
            activity_name = get_field_text(act.get("fields", {}), f["activity_name"])
            if not client.update_record(_cfg["tables"]["activity_table_id"],
                                        act.get("record_id"), {f["onetoone_flag"]: "否"}):
                return {"code": "WRITE_FAILED", "activity_id": activity_id,
                        "name": activity_name, "onetoone_flag": "", "enabled": False}
            log(f"管理员设置一对一功能开关: {activity_name}({activity_id}) -> 否")
            return {"code": "OK", "activity_id": activity_id, "name": activity_name,
                    "onetoone_flag": "否", "enabled": False}
    except GroupingBusy:
        return {"code": "BUSY", "activity_id": activity_id, "name": name_hint,
                "onetoone_flag": "", "enabled": False}
