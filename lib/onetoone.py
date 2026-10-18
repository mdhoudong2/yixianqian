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
import collections
from concurrent.futures import ThreadPoolExecutor

from lib import grouping as lib_grouping
from lib import recommend
from lib.bitable_client import get_field_number, get_field_text, get_multi_select_value
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
    "oto_target_oid", "oto_target_name", "oto_round",
    "signup_activity_id", "signup_openid", "signup_nickname", "signup_status",
    "signup_attendance",
    "gender", "user_id", "real_name",
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
# 线下一对一（女生坐镇、男生上门）均衡分配：一条边是双向奔赴，双方优先级对称各占 0.4、
# 资料相似度 0.2，再用运输问题求「每个女生恰好 K 场、男生配额均衡」下的总分最大。
WEIGHT_OFFLINE_PRIORITY = 0.4

# 一对一执行/清空结果的互斥走 lib.grouping.grouping_lock（跨进程 flock，bot 与 H5
# 共用同一批 yixianqian_grouping_<活动>.lock）。等待预算：执行/清空是分钟级作业，
# 与分组同口径；用户提交要快速失败，不占线程池。
EXECUTE_LOCK_TIMEOUT = 180
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


class _MinCostFlow:
    """SPFA 连续最短路增广的最小费用最大流（用于线下均衡分配的运输问题求解）。"""

    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, cost):
        # 正向边 + 反向边；正向边 rev 指向反边在 g[v] 中的位置
        self.g[u].append([v, cap, cost, len(self.g[v])])
        self.g[v].append([u, 0, -cost, len(self.g[u]) - 1])

    def run(self, s, t, max_flow):
        flow = 0
        total_cost = 0
        INF = float("inf")
        while flow < max_flow:
            dist = [INF] * self.n
            inq = [False] * self.n
            prev_v = [-1] * self.n
            prev_e = [-1] * self.n
            dist[s] = 0
            dq = collections.deque([s])
            inq[s] = True
            while dq:
                u = dq.popleft()
                inq[u] = False
                for i, (v, cap, cost, _rev) in enumerate(self.g[u]):
                    if cap > 0 and dist[v] > dist[u] + cost:
                        dist[v] = dist[u] + cost
                        prev_v[v] = u
                        prev_e[v] = i
                        if not inq[v]:
                            dq.append(v)
                            inq[v] = True
            if dist[t] == INF:
                break
            d = max_flow - flow
            v = t
            while v != s:
                d = min(d, self.g[prev_v[v]][prev_e[v]][1])
                v = prev_v[v]
            flow += d
            total_cost += d * dist[t]
            v = t
            while v != s:
                e = self.g[prev_v[v]][prev_e[v]]
                e[1] -= d
                self.g[v][e[3]][1] += d
                v = prev_v[v]
        return flow, total_cost


def run_offline_onetoone_matching(participants, selections, profiles, female_chats):
    """线下均衡分配：女生坐镇各聊 female_chats 场，男生按配额上门，总分最大。

    与线上「各自独立 top-N」不同，线下是一场运输问题（b-matching）：
      - 每个女生恰好 female_chats 场（女生坐镇不动，男生来找她）；
      - 每个男生 base 或 base+1 场，base = floor(F×K / M)，总和 = F×K；
      - 每条边（男↔女）最多聊一次；
      - 目标：所有配对的总分最大（双向奔赴对称分）。

    participants / selections / profiles 形状与 run_onetoone_matching 完全一致。
    对称分 = WEIGHT_OFFLINE_PRIORITY × (男对女优先级得分 + 女对男优先级得分)
           + WEIGHT_ONETOONE_MATCH × 资料相似度
    即 0.4×男对女 + 0.4×女对男 + 0.2×资料相似度，未选择某方记 ONETOONE_UNSELECTED_SCORE(30)。

    返回: {
      "male_lists":   {男 open_id: [(女 open_id, rank, score), ...]}，rank 从 1 起、按得分降序；
      "female_lists": {女 open_id: [(男 open_id, score), ...]}，按得分降序；
      "male_quota":   {男 open_id: 该男共几场}，
    }
    """
    males = [p["id"] for p in participants if p.get("gender") == "male"]
    females = [p["id"] for p in participants if p.get("gender") == "female"]
    M = len(males)
    F = len(females)
    K = int(female_chats)
    if M == 0 or F == 0:
        return {"male_lists": {}, "female_lists": {}, "male_quota": {}}
    if K < 1:
        raise ValueError(f"女生场次 female_chats 必须 ≥ 1，当前 {K}")
    if K > M:
        raise ValueError(
            f"女生场次 female_chats={K} 不能超过男生人数 {M}（每个女生最多和 {M} 位男生各聊一次）")

    # 我的选择：selector -> {target: 最小优先级（最高意愿）}
    my_choice = {}
    for sel_oid, choices in selections.items():
        mc = {}
        for c in choices:
            if c["id"] not in mc or c["priority"] < mc[c["id"]]:
                mc[c["id"]] = c["priority"]
        my_choice[sel_oid] = mc

    def prio(a, b):
        p = my_choice.get(a, {}).get(b)
        return PRIORITY_SCORES.get(p, ONETOONE_UNSELECTED_SCORE)

    # 对称分矩阵 score[i][j]（男 i × 女 j）
    score = [[0.0] * F for _ in range(M)]
    for i, m in enumerate(males):
        pm = profiles.get(m, {})
        for j, f in enumerate(females):
            try:
                mch = recommend.match_score(pm, profiles.get(f, {}))[0]
            except Exception:
                mch = 0
            score[i][j] = (WEIGHT_OFFLINE_PRIORITY * (prio(m, f) + prio(f, m))
                           + WEIGHT_ONETOONE_MATCH * mch)

    # 男生配额：base 或 base+1，总和 = F×K
    E = F * K
    base, rem = divmod(E, M)
    quota = [base + 1 if i < rem else base for i in range(M)]

    # 建图：S=0，男=1..M，女=1+M..M+F，T=1+M+F
    S = 0
    male_base = 1
    female_base = 1 + M
    T = 1 + M + F
    mf = _MinCostFlow(T + 1)
    for i in range(M):
        mf.add_edge(S, male_base + i, quota[i], 0)
    mf_edges = [[None] * F for _ in range(M)]
    for i in range(M):
        for j in range(F):
            cost = -int(round(score[i][j] * 1000))  # 最大化总分 → 最小化负分
            mf.add_edge(male_base + i, female_base + j, 1, cost)
            mf_edges[i][j] = mf.g[male_base + i][-1]  # 正向边引用，之后读残余容量
    for j in range(F):
        mf.add_edge(female_base + j, T, K, 0)

    flow, _ = mf.run(S, T, E)
    if flow != E:
        raise RuntimeError(f"线下均衡分配未收敛：实际配对 {flow}/{E} 条")

    male_lists = {}
    female_lists = {}
    male_quota = {}
    for i, m in enumerate(males):
        lst = []
        for j in range(F):
            if mf_edges[i][j][1] == 0:  # 正向边被用满 → 该边入选
                lst.append((females[j], score[i][j]))
        lst.sort(key=lambda x: (-x[1], x[0]))
        male_lists[m] = [(fid, rank, sc) for rank, (fid, sc) in enumerate(lst, 1)]
        male_quota[m] = quota[i]

    for j, f in enumerate(females):
        lst = []
        for i in range(M):
            if mf_edges[i][j][1] == 0:
                lst.append((males[i], score[i][j]))
        lst.sort(key=lambda x: (-x[1], x[0]))
        female_lists[f] = lst

    return {"male_lists": male_lists, "female_lists": female_lists, "male_quota": male_quota}


def _offline_result_to_lists(offline):
    """均衡结果 → {oid: [(target_oid, rank), ...]}（与 run_onetoone_matching 同形）。

    male_lists 已带 rank；female_lists 按得分降序补 rank=1..。用于复用 execute 的
    结果写表循环，不改变 fetch_result / H5 结果页的读取口径。
    """
    out = {}
    for m, lst in offline["male_lists"].items():
        out[m] = [(fid, rank) for fid, rank, _sc in lst]
    for f, lst in offline["female_lists"].items():
        out[f] = [(mid, rank) for rank, (mid, _sc) in enumerate(lst, 1)]
    return out


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
    """取该活动的报名者：[{open_id, nickname, gender, user_id, real_name, attendance}]。

    读取失败（raw client 返回 None）按空表处理：这些数据只用于名单展示/候选校验，
    飞书抖一下不该让整条指令失败——与迁移前 bot 经 lib.grouping.collect_participants
    的兜底口径一致。性别/用户ID 走调用方注入的 find_user_by_openid。
    real_name = 用户表「姓名」去空白（现场活动显示名：非空优先于昵称，由渲染层决定）。
    attendance = 报名记录上的「签到状态」原值（按时/迟到/未到/空）。
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
        attendance = get_field_text(sf, f["signup_attendance"])
        if oid:
            signup_rows.append((oid, nickname, attendance))

    def _fetch(row):
        oid, nickname, attendance = row
        gender = ""
        uid = ""
        real_name = ""
        user_recs = find_user_by_openid(oid)
        if user_recs:
            uf = user_recs[0].get("fields", {})
            gender = get_field_text(uf, f["gender"])
            uid = get_field_text(uf, f["user_id"])
            real_name = get_field_text(uf, f["real_name"]).strip()
        return {"open_id": oid, "nickname": nickname, "gender": gender, "user_id": uid,
                "real_name": real_name, "attendance": attendance}

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


def round_of(fields):
    """结果行的轮次（int）。

    空 / 非「1..N」的数字一律视为**第 1 轮**：多轮上线时测试 base 的既有结果由
    迁移脚本回填为「1」，未迁移环境（桩表、旧数据）读到空串；空按第 1 轮处理，
    才能与迁移前「有任何结果即拦重跑」的语义一致（parity 保护面，见
    tests/test_onetoone_parity.py 与 lib.onetoone.execute 的幂等检查）。
    """
    raw = get_field_text(fields or {}, _cfg["fields"]["oto_round"])
    return int(raw) if raw.isdigit() and int(raw) >= 1 else 1


# ---------------------------------------------------------------- 读接口

def fetch_status(activity_id, *, client, activity, find_user_by_openid):
    """一对一状态：管理台要的那几个数字 + 已生成轮次。"""
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
    result_rows = client.search_records(_cfg["tables"]["onetoone_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]}],
    }) or []
    rounds = sorted({round_of(r.get("fields", {})) for r in result_rows})
    return {
        "code": "OK",
        "activity_id": activity_id,
        "name": name,
        "status": status,
        "signup_count": len(signups),
        "submitted_count": len(submitted_oids),
        "rounds": rounds,
        "latest_round": rounds[-1] if rounds else None,
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
            "real_name": s.get("real_name", ""),
            "attendance": s.get("attendance", ""),
            "checked_in": lib_grouping.is_checked_in(s.get("attendance", "")),
        })

    return {
        "code": "OK",
        "activity_id": activity_id,
        "name": act_name,
        "total": len(unsubmitted),
        "unsubmitted": unsubmitted,
        "all_submitted": not unsubmitted,
        "signin_enabled": lib_grouping.signin_enabled(activity.get("fields", {})),
    }


def preview(activity_id, round_no, *, client, activity, find_user_by_openid, female_chats=None):
    """执行前的影响面预览——**一个栏位都不写**（对照 lib.grouping.preview_round）。

    独立于 execute 的纯读路径：管理员只是「看一眼」，绝不能产生任何副作用。
    round_no：本次拟执行的轮次。`existing_round_results` 是该轮已有结果数（非 0 时
    执行会被幂等拦住）；`rounds_preserved` 等字段回答该活动现有历史轮次与结果量。
    `female_chats`：女生场次 K；缺/非法/超男生数时进 blockers，不让前端确认执行。
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
    _selection_participants, _selections, skipped = _scan_selections(rows)

    # 与 execute 同口径：参与者 = 全部「已报名」者（含未提交志愿的人）。
    signups = build_participants(rec_activity_id, client=client,
                                 find_user_by_openid=find_user_by_openid)
    participants = [{"id": s["open_id"],
                     "gender": "male" if s["gender"] == "男性" else "female"}
                    for s in signups if s["gender"] in ("男性", "女性")]

    # 与 execute 同一把「现场签到执行池」闸刀：签到开启时预览人数/场次也必须按
    # 「已到」算，否则管理员按预览的男生数选 K，执行时会被 FEMALE_CHATS_INVALID 拦下。
    signin_blocker = None
    if lib_grouping.signin_enabled(af):
        attendance = lib_grouping.attendance_map(rec_activity_id, client=client)
        if attendance is None:
            signin_blocker = "签到名单读取失败，执行会被拦截（SIGNIN_QUERY_FAILED），请稍后重试"
        else:
            participants = [p for p in participants
                            if lib_grouping.is_checked_in(attendance.get(p["id"]))]

    male_count = sum(1 for p in participants if p["gender"] == "male")
    female_count = sum(1 for p in participants if p["gender"] == "female")

    existing = client.search_records(_cfg["tables"]["onetoone_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [rec_activity_id]}],
    }) or []
    rounds = sorted({round_of(r.get("fields", {})) for r in existing})
    this_round = [r for r in existing if round_of(r.get("fields", {})) == round_no]

    blockers = []
    if signin_blocker:
        blockers.append(signin_blocker)
    if not participants:
        blockers.append("没有已报名且性别有效的参与者")
    if this_round:
        blockers.append(f"第{round_no}轮已生成一对一结果，请勿重复执行")
    if (not isinstance(round_no, int) or isinstance(round_no, bool)
            or round_no < 1):
        blockers.append(f"轮次必须是 ≥1 的整数，当前 {round_no}")
    # 女生场次 K 校验（与 execute 进门同口径）
    if female_chats is None:
        blockers.append("请指定女生场次（每位女生接待几位男生）")
    elif (not isinstance(female_chats, int) or isinstance(female_chats, bool)
            or female_chats < 1):
        blockers.append(f"女生场次必须是 ≥1 的整数，当前 {female_chats}")
    elif male_count and female_chats > male_count:
        blockers.append(f"女生场次 {female_chats} 超过男生人数 {male_count}"
                        f"（每个女生最多和每位男生各聊一次）")

    total_pairs_expected = 2 * female_count * female_chats if (
        isinstance(female_chats, int) and not isinstance(female_chats, bool)
        and female_chats >= 1) else 0

    return {
        "code": "OK",
        "activity_id": activity_id,
        "name": name,
        "onetoone_status": status,
        "round": round_no,
        "participant_count": len(participants),
        "male_count": male_count,
        "female_count": female_count,
        "female_chats": female_chats,
        "total_pairs_expected": total_pairs_expected,
        "skipped_no_gender": skipped,
        # 旧字段语义不动：始终回答「清空一对一结果会删掉多少历史结果」。
        "existing_results": len(existing),
        "existing_round_results": len(this_round),
        "existing_rounds": rounds,
        "historical_results_to_clear": len(existing),
        "historical_rounds_to_clear": rounds,
        # 加法字段：执行本轮不清历史时保留的轮次与结果条数。
        "rounds_preserved": rounds,
        "results_preserved": len(existing),
        "can_execute": not blockers,
        "blockers": blockers,
    }


def fetch_result(activity_id, round_no, *, client, activity):
    """读取某活动某轮次的一对一必聊结果（与管理台端点/执行结果同构）。

    `round_no=None` 时取最新轮次。结果由 bot/H5 执行写入，调用端必须用直读 client
    （实时读飞书），不吃快照——轮次切换要立刻可见。

    返回 {"activity_id", "name", "round", "rounds", "people", "result_count",
    "matched_count"}；对应轮次没有记录时 people=[]（round 为 None 或调用方指定的
    轮次），由调用端决定怎么呈现。person.partners 按 rank 升序；people 按 open_id
    排序（确定性；user_id 等展示字段由调用端补全，lib 不知道用户表）。
    """
    _require_configured()
    f = _cfg["fields"]
    af = (activity or {}).get("fields", {})
    rows = client.search_records(_cfg["tables"]["onetoone_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]}],
    }) or []

    rounds = sorted({round_of(r.get("fields", {})) for r in rows})
    wanted = int(round_no) if round_no is not None else (rounds[-1] if rounds else None)

    people = {}
    result_count = 0
    if wanted is not None:
        for r in rows:
            sf = r.get("fields", {})
            if round_of(sf) != wanted:
                continue
            oid = get_field_text(sf, f["oto_user_oid"])
            if not oid:
                continue
            result_count += 1
            person = people.get(oid)
            if person is None:
                person = {
                    "open_id": oid,
                    "nickname": get_field_text(sf, f["oto_user_name"]),
                    "gender": get_field_text(sf, f["oto_user_gender"]),
                    "partners": [],
                }
                people[oid] = person
            target_oid = get_field_text(sf, f["oto_target_oid"])
            if target_oid:
                person["partners"].append({
                    "open_id": target_oid,
                    "nickname": get_field_text(sf, f["oto_target_name"]),
                    "rank": int(get_field_number(sf, f["oto_rank"], 0)),
                })

    ordered = sorted(people.values(), key=lambda p: p["open_id"])
    for person in ordered:
        person["partners"].sort(key=lambda x: x["rank"])

    return {
        "activity_id": activity_id,
        "name": get_field_text(af, f["activity_name"]),
        "round": wanted,
        "rounds": rounds,
        "people": ordered,
        "result_count": result_count,
        "matched_count": len(ordered),
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


def latest_round(client, activity_id):
    """该活动一对一结果的最新轮次（int）；没有任何结果时回 0；读失败回 None。

    轮次默认值 =「最新已有轮次 + 1」的唯一推演入口：bot 执行指令与 H5
    run/preview 端点共用，避免各算各的。空/非正整数轮次行按第 1 轮处理
    （round_of 归一化口径：迁移前的空轮次遗留行 = 第 1 轮）。

    读失败（raw client 回 None）与「没有结果」必须严格区分：调用端拿到 None
    必须明报失败，绝不能拿 0 当「没跑过」——那等于用一次限流换掉一份既有结果。
    """
    _require_configured()
    rows = client.search_records(_cfg["tables"]["onetoone_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": _cfg["fields"]["oto_activity_id"], "operator": "is",
                        "value": [activity_id]}]})
    if rows is None:
        return None
    rounds = [round_of(r.get("fields", {})) for r in rows]
    return max(rounds) if rounds else 0


def reset_onetoone_results(activity_id, *, client, activity, lock_timeout=None):
    """清空该活动全部一对一结果，并把「一对一状态」清空回未开始（写空字串）。

    整体持 grouping_lock(activity_id)（**自持锁**，调用方不得再持同一把锁——
    锁不可重入）。

    fail-closed（原 start_collection「清结果」能力的收编，不弱化）：结果表读失败
    → RESULTS_SEARCH_FAILED；删除数不足 → DELETE_INCOMPLETE；状态栏位清空写失败 →
    STATUS_FAILED。报成功会让管理员以为可以重跑，实际被残留结果卡住（重跑同轮次
    会被 ALREADY_DONE 拦）。

    只清该侧结果与状态：不碰分组结果/状态，不碰报名与志愿记录。
    成功回传 cleared_results。
    """
    if lock_timeout is None:
        lock_timeout = EXECUTE_LOCK_TIMEOUT
    _require_configured()
    f = _cfg["fields"]
    t = _cfg["tables"]
    af = activity.get("fields", {})
    activity_name = get_field_text(af, f["activity_name"])
    record_id = activity.get("record_id")
    base = {"activity_id": activity_id, "activity_name": activity_name}
    try:
        with grouping_lock(activity_id, timeout=lock_timeout):
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
                log(f"已清空 {deleted} 条历史一对一结果: {activity_name}")
            if not client.update_record(t["activity_table_id"], record_id,
                                        {f["onetoone_status"]: ""}):
                return dict(base, code="STATUS_FAILED", cleared_results=deleted)
            log(f"管理员清空一对一结果: {activity_name}")
            return dict(base, code="OK", cleared_results=deleted)
    except GroupingBusy:
        return dict(base, code="BUSY")


def execute(activity_id, round_no, *, client, activity, find_user_by_openid, lock_timeout=None,
            female_chats=None):
    """执行一对一匹配（幂等检查 + 读入参 + 写结果）。

    `round_no`：写入与幂等都锚定「(活动, 轮次)」——同一轮重跑被 ALREADY_DONE
    拦住，换一轮即可产出第 N+1 轮（与 lib.grouping.execute_round 同语义）。
    `round_no` 必须是不小于 1 的整数，越界直接回 ROUND_OUT_OF_RANGE，
    零写入、零锁等待（preview 的 blockers 只是提示，bot/H5 都不能拿它当门禁）。

    `female_chats`：女生坐镇、男生上门的均衡分配里「每个女生聊几场」（K）。
    运行时必填，≥1 的整数；缺省回 FEMALE_CHATS_REQUIRED、非法回 FEMALE_CHATS_INVALID
    （均为零写入）。K > 男生人数在锁内读报名后由算法 ValueError 收口。

    **调用方不需要持锁**：本函数整体持 lib.grouping.grouping_lock（跨进程 flock，
    与 H5/分组共用同一批活动锁）。状态机每一步的读失败（raw client 返回 None）
    都在写入之前中止，**失败一律零状态变更**（不再有「已截止」前置写，也不再有
    「恢复收集中」的恢复写）：

    ① 幂等查该活动该轮历史结果，None → 中止（零写入）；
    ② 读志愿，None → 中止并明说失败（零写入）；
    ③ 签到开启时读签到名单，None → 中止（零写入）；
    ④ 空参与者 → 维持原「没有参与者」文案（零写入）；
    ⑤ 算法异常（含 K 非法）→ 中止（零写入）；
    ⑥ 写结果，`created < total` → 回查：None → 不得宣称已回滚；回查成功 → 删除
       本轮的部分写入并检查删除数（活动表一个栏位都不写）；
    ⑦ 成功 → 状态「已完成」（检查返回值；结果已写但状态写失败明报 DONE_STATUS_FAILED）。

    返回 code：OK / BUSY / ROUND_OUT_OF_RANGE / SEARCH_FAILED / ALREADY_DONE /
    SELECTIONS_SEARCH_FAILED / SIGNIN_QUERY_FAILED / NO_PARTICIPANTS /
    ALGORITHM_ERROR / FEMALE_CHATS_REQUIRED / FEMALE_CHATS_INVALID / PARTIAL_WRITE /
    PARTIAL_WRITE_VERIFY_FAILED / PARTIAL_WRITE_INCOMPLETE / DONE_STATUS_FAILED。
    """
    if lock_timeout is None:
        lock_timeout = EXECUTE_LOCK_TIMEOUT
    _require_configured()
    f = _cfg["fields"]
    activity_name = get_field_text(activity.get("fields", {}), f["activity_name"])
    base = {"activity_id": activity_id, "round": round_no, "activity_name": activity_name}
    # 写入路径的轮次收口：轮次字段已转文本、不设上限，只挡非「≥1 整数」。
    if (not isinstance(round_no, int) or isinstance(round_no, bool)
            or round_no < 1):
        return dict(base, code="ROUND_OUT_OF_RANGE")
    # 女生场次收口（零写入）：缺省 / 非正整数都直接回，别让管理员跑完才发现没配对。
    if female_chats is None:
        return dict(base, code="FEMALE_CHATS_REQUIRED")
    if (not isinstance(female_chats, int) or isinstance(female_chats, bool)
            or female_chats < 1):
        return dict(base, code="FEMALE_CHATS_INVALID", female_chats=female_chats)
    # 动作层互斥：执行一对一前对面分组 flag=「是」→ 拒绝（不自动动对面数据）。
    # 假表缺分组栏位 → 不触发；锁前判定，命中时零写入零等待。
    if lib_grouping.counterpart_execute_active(activity, flag_field=f["group_flag"]):
        return dict(base, code="EXCLUSIVE_MODE", blocked_by="group")
    try:
        with grouping_lock(activity_id, timeout=lock_timeout):
            return _execute_locked(activity_id, round_no, client=client, activity=activity,
                                   find_user_by_openid=find_user_by_openid,
                                   female_chats=female_chats)
    except GroupingBusy:
        return dict(base, code="BUSY")


def _execute_locked(activity_id, round_no, *, client, activity, find_user_by_openid,
                    female_chats=None):
    _require_configured()
    f = _cfg["fields"]
    t = _cfg["tables"]
    af = activity.get("fields", {})
    activity_name = get_field_text(af, f["activity_name"])
    record_id = activity.get("record_id")
    base = {"activity_id": activity_id, "round": round_no, "activity_name": activity_name}

    # ① 幂等保护：该活动本轮已生成过结果则拒绝重跑（多轮可轮次递进）。读失败
    # （None）中止、零写入——把「读不到」当「没跑过」去重跑，等于用一次限流换掉
    # 一份既有结果。轮次比较在内存里做（round_of），第 1 轮同时覆盖迁移前的
    # 空轮次遗留行：搜「轮次 is 1」在飞书侧匹配不到空串，只有归一化才等价。
    existing_results = client.search_records(t["onetoone_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is", "value": [activity_id]}],
    })
    if existing_results is None:
        return dict(base, code="SEARCH_FAILED")
    if any(round_of(r.get("fields", {})) == round_no for r in existing_results):
        return dict(base, code="ALREADY_DONE")

    # 后续所有读写都用活动记录自己的 ID，不退回入参：入参可能是用户随手敲的别名
    rec_activity_id = get_field_text(af, f["activity_id"]) or ""

    # ② 读志愿记录：读失败中止（零写入），如实报告
    selections_records = client.search_records(t["onetoone_select_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["oto_activity_id"], "operator": "is",
                        "value": [rec_activity_id]}],
    })
    if selections_records is None:
        return dict(base, code="SELECTIONS_SEARCH_FAILED")
    _selection_participants, selections, skipped = _scan_selections(selections_records)

    # 参与者 = 全部「已报名」者（含未提交志愿的人）——线下「女生坐镇」必须人人有
    # 位，未提交志愿的女生按匹配度 + 对方意愿参与，绝不坐冷板凳。性别缺失者排除。
    signups = build_participants(rec_activity_id, client=client,
                                 find_user_by_openid=find_user_by_openid)
    participants = [{"id": s["open_id"],
                     "gender": "male" if s["gender"] == "男性" else "female"}
                    for s in signups if s["gender"] in ("男性", "女性")]

    # ③ 现场签到执行池过滤（设计 §3.3/§3.4，与 lib.grouping.execute_round 同一
    # 兜底）：只挡提交挡不住「早提交、活动当天未到」；读失败（None）fail-closed
    # 零写入，绝不带着「读不到」把全部人放进来。
    if lib_grouping.signin_enabled(af):
        attendance = lib_grouping.attendance_map(rec_activity_id, client=client)
        if attendance is None:
            return dict(base, code="SIGNIN_QUERY_FAILED")
        participants = [p for p in participants
                        if lib_grouping.is_checked_in(attendance.get(p["id"]))]

    # ④ 没有参与者：维持原文案（零写入）
    if not participants:
        return dict(base, code="NO_PARTICIPANTS", skipped=skipped)

    profiles = build_profiles([p["id"] for p in participants],
                              find_user_by_openid=find_user_by_openid)

    # ⑤ 算法异常（含 K 非法）中止（零写入）
    try:
        offline = run_offline_onetoone_matching(participants, selections, profiles,
                                                female_chats)
        matches = _offline_result_to_lists(offline)
    except ValueError as e:
        return dict(base, code="FEMALE_CHATS_INVALID", error=str(e))
    except Exception as e:
        return dict(base, code="ALGORITHM_ERROR", error=str(e))

    # 报名信息：open_id -> 显示名（现场活动统一「真名（缺省昵称）」，姓名去空白非空才用）
    oid_to_name = {s["open_id"]: (s["real_name"] or s["nickname"]) for s in signups}
    result_records = []
    for p in participants:
        pid = p["id"]
        for target_oid, rank in matches.get(pid, []):
            result_records.append({
                f["oto_activity_id"]: rec_activity_id,
                f["oto_rank"]: rank,
                f["oto_user_oid"]: pid,
                f["oto_user_name"]: oid_to_name.get(pid, ""),
                f["oto_user_gender"]: "男性" if p["gender"] == "male" else "女性",
                f["oto_target_oid"]: target_oid,
                f["oto_target_name"]: oid_to_name.get(target_oid, ""),
                f["oto_round"]: str(round_no),
            })

    if result_records:
        created = client.batch_create_records(t["onetoone_result_table"], result_records)
        if created < len(result_records):
            # ⑥ 写入不完整：回查已写入的部分结果并回滚（只回滚本轮——历史轮次
            # 的结果不是这次写坏的，不能一起删）。活动表一个栏位都不写。
            # 回查失败（None）时**不得宣称已回滚**——可能残留在表里，谎报成功
            # 会把管理员的后续重试引向「结果重复」或「被幂等卡死」。
            partial = client.search_records(t["onetoone_result_table"], {
                "conjunction": "and",
                "conditions": [{"field_name": f["oto_activity_id"], "operator": "is",
                                "value": [rec_activity_id]}],
            })
            if partial is None:
                return dict(base, code="PARTIAL_WRITE_VERIFY_FAILED", created=created,
                            total=len(result_records))
            this_round = [p for p in partial
                          if round_of(p.get("fields", {})) == round_no]
            deleted = 0
            if this_round:
                deleted = client.batch_delete_records(
                    t["onetoone_result_table"], [p["record_id"] for p in this_round])
            if deleted < len(this_round):
                return dict(base, code="PARTIAL_WRITE_INCOMPLETE", created=created,
                            total=len(result_records), deleted=deleted,
                            residual=len(this_round) - deleted)
            return dict(base, code="PARTIAL_WRITE", created=created,
                        total=len(result_records))

    # ⑦ 成功 → 状态「已完成」（检查返回值：结果已写但状态写失败必须明说）
    total_pairs = sum(len(v) for v in matches.values())
    if not client.update_record(t["activity_table_id"], record_id,
                                {f["onetoone_status"]: "已完成"}):
        return dict(base, code="DONE_STATUS_FAILED", participant_count=len(participants),
                    total_pairs=total_pairs)

    log(f"第{round_no}轮一对一匹配完成: {activity_name}, "
        f"{len(participants)}人, {total_pairs}条必聊")
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
    """开启/关闭活动的「一对一功能开启」开关（控制 H5「我的活动」/活动页的志愿与结果入口）。

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

    `activity` 由调用端在锁外解析好（网络读不压在锁里），产品调用端一律显式传入：
    真实 base 的「活动ID」是自动编号字段，按字符串过滤必回 0 行。`activity=None`
    时在锁内查一次——该路径仅供桩表/测试使用、真表不可用（只会得到 NOT_FOUND），
    不是产品路径。写失败如实回 WRITE_FAILED（检查 update_record 返回值，不报成功）。
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
