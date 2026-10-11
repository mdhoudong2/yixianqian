"""活动分组：跨进程互斥锁 + 分组业务逻辑（bot 与 H5 后端共用）。

本模块**不得** import bot/ 或 web/backend/ 下的任何模块（constants / config /
clients / queries）：CI 的 conftest 把仓库根和 bot/ 都插进 sys.path，`import config`
在两端会解析到不同的文件，等于把「读错 base token」变成定时炸弹。表 ID、栏位名、
锁目录一律由两端启动时各自 `configure()` 注入。

对外一律回传 dict、不回传字串：文案归渲染层。H5 要的是结构化数据，让它去
反解析「人数不足，无法分组：3男5女…」这种句子，文案一改就炸。
"""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from lib.bitable_client import get_field_number, get_field_text
from lib.util import log

try:
    import fcntl
except ImportError:  # 无 flock 的平台退化为仅线程锁，与 lib/storage 同一取舍
    fcntl = None


# ---------------------------------------------------------------- 互斥锁

class GroupingBusy(Exception):
    """该活动的分组互斥锁被别人持着，本次拿不到。"""


_lock_dir = None


def configure(*, activity_table_id=None, group_select_table=None, group_result_table=None,
              signup_table_id=None, fields=None, lock_dir=None):
    """注入表 ID、栏位名与锁目录。两端启动时各调一次。

    表 ID / 栏位名没有默认值：给个「猜出来的默认值」比报错危险得多——写错 base 时
    不会有任何异常，只会把分组结果写进另一张表。少传就在用到时报 RuntimeError。
    只用锁的部分（锁测试、H5 的纯预览）可以只传 lock_dir。
    """
    global _lock_dir
    unknown = [k for k in (fields or {}) if k not in _REQUIRED_FIELDS]
    if unknown:
        raise RuntimeError(f"lib.grouping.configure 收到未知栏位名：{unknown}")
    if fields:
        merged = {**_cfg["fields"], **fields}
        missing = [k for k in _REQUIRED_FIELDS if k not in merged]
        if missing:
            raise RuntimeError(f"lib.grouping.configure 缺少栏位名：{missing}")
        _cfg["fields"] = merged
    for key, value in (("activity_table_id", activity_table_id),
                       ("group_select_table", group_select_table),
                       ("group_result_table", group_result_table),
                       ("signup_table_id", signup_table_id)):
        if value is not None:
            _cfg["tables"][key] = value
    if lock_dir is not None:
        _lock_dir = lock_dir


def _sanitize(activity_id):
    """活动 ID 转安全文件名。

    activity_id 来自聊天输入 / URL，白名单外的字符全部替换掉才拼路径，
    否则「../../etc/passwd」这种输入能把锁文件写到数据目录外面去。
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(activity_id))[:64]


_locks_guard = threading.Lock()
# path -> [threading.Lock, 在用计数]。值是强引用，见 _thread_lock 的说明。
_thread_locks = {}
_MAX_THREAD_LOCKS = 256


def _thread_lock(path):
    """取该路径的进程内锁，并登记「有人在用」。

    调用方**必须**在临界区（含等 flock）结束后调 _unpin_thread_lock(path)，否则
    这把锁永远淘汰不掉。挡掉的是最常见的「同一进程的 4 条管理指令线程」竞争。

    为什么不用 WeakValueDictionary（审查点名的坑）：弱引用下锁对象在 with 结束、
    局部变量消失后就可能被 GC 回收，同一路径下一次进来会拿到一把**全新的**锁，
    「同一路径两线程互斥」静默失效——比多留几十 bytes 危险得多。强引用下只要还
    可能有人在用，字典就是它的强引用，GC 不会动它。

    为什么要上限：bot 是常驻进程，每见过一个活动 ID 就永久多一把锁，是无界结构。
    但单纯「按插入顺序删没在用的」有竞态：另一个线程可能刚拿到引用、还没 acquire，
    这时它手上的锁被删掉换成新锁，两者同时「持有」。所以淘汰的判据是**在用计数**
    而不是 locked()：每次取锁都 +1、用完才 -1，计数归零才保证没有线程还攥着引用。
    """
    with _locks_guard:
        entry = _thread_locks.get(path)
        if entry is None:
            entry = [threading.Lock(), 0]
            _thread_locks[path] = entry
        entry[1] += 1
        return entry[0]


def _unpin_thread_lock(path):
    """解除登记，并顺带把超过上限、且无人再用的锁清掉。"""
    with _locks_guard:
        entry = _thread_locks.get(path)
        if entry is None:
            return
        entry[1] -= 1
        if len(_thread_locks) <= _MAX_THREAD_LOCKS:
            return
        for old_path in [k for k, v in _thread_locks.items() if v[1] == 0]:
            del _thread_locks[old_path]
            if len(_thread_locks) <= _MAX_THREAD_LOCKS:
                break


def _flock_wait(f, deadline, activity_id):
    """等跨进程 flock，等到截止时间为止。

    抢到回 True（调用方据此决定要不要 LOCK_UN）；抢不到抛 GroupingBusy，**不会**回
    False——没拿到的锁不必去解，否则 finally 会对一把从未持有的 fd 调 LOCK_UN。

    用 LOCK_NB 轮询而不是阻塞版 flock：阻塞版没有逾时，而分组是分钟级作业，
    对端要是卡住不动，一个 flock 就足以把调用方的执行线程池（bot 4 条、gunicorn
    16 条）永久占死——那正是「逾时必填」想避免的失败模式。轮询只发生在真正的
    跨进程争用上，进程内的常规竞争早被线程锁挡掉了。
    """
    if not fcntl:
        return False
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GroupingBusy(activity_id)
            time.sleep(min(0.2, remaining))


@contextmanager
def grouping_lock(activity_id, timeout):
    """按活动分档的互斥锁：进程内 threading.Lock + 跨进程 flock。

    分档而不是全局一把：A 场分组不该把 B 场的管理员一起挡在门外。

    `timeout` 必填，且是**总的**等待预算（线程锁与 flock 共用同一截止时间）。
    分组是分钟级作业（200 人光读志愿就有 40 秒下限，全局 10 req/s 令牌桶），
    无限等待会把调用方的执行线程池占满。抢不到就抛 GroupingBusy，让调用端决定
    怎么告诉管理员——可观察的失败优于不可观察的挂死。

    不可重入：同一线程重复进入会死锁。这是刻意的——重入说明调用方搞错了锁的粒度，
    宁可死锁也不要静默退化成「看起来加了锁其实没加」。
    """
    if not _lock_dir:
        raise RuntimeError("lib.grouping 未 configure(lock_dir=...)，不能使用分组互斥锁")
    path = os.path.join(_lock_dir, f"yixianqian_grouping_{_sanitize(activity_id)}.lock")
    deadline = time.monotonic() + timeout if timeout > 0 else time.monotonic()
    tl = _thread_lock(path)
    try:
        # 进程内先挡，剩下的预算拿去等 flock。注意 timeout 不能原样传给 acquire()：
        # threading 把负数当「无限等待」，正好把要防的情况放进来。
        if not tl.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise GroupingBusy(activity_id)
        # 锁文件与数据分离（同 lib/storage._FileLock）：锁不能跟着数据被 rename 走。
        try:
            f = open(path, "a+")
            try:
                held = _flock_wait(f, deadline, activity_id)
                try:
                    yield
                finally:
                    # 只在真的抢到时才解锁：GroupingBusy 是从 _flock_wait 抛出来的，
                    # 那时这把 fd 上的 flock 并不属于我们。
                    if held:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()
        finally:
            # open() 自己抛错（锁目录没了等）也必须走到这里，否则这把进程内锁
            # 永远不释放，后面同活动的分组全部超时。
            tl.release()
    finally:
        # 顺序不能反：先 release 再解除登记。反过来的话登记已清而锁还攥在手里，
        # 并发的淘汰会把这条路径删掉、下一个调用者新建一把锁，两把同时「持有」。
        _unpin_thread_lock(path)


# ---------------------------------------------------------------- 运行配置

# 栏位名用语义键（不是 bot 的 FIELD_xxx、也不是 web 的 F_XXX）：lib 不该知道
# 任何一端的命名习惯，两端各自把自己的常量翻译过来。
_REQUIRED_FIELDS = (
    "activity_id", "activity_name",
    "group_status", "male_per_group", "female_per_group", "group_flag",
    "gs_activity_id", "gs_selector_oid", "gs_selector_gender", "gs_choices",
    "gr_activity_id", "gr_group_no", "gr_user_oid", "gr_user_name",
    "gr_user_gender", "gr_round",
    "signup_activity_id", "signup_status", "signup_openid", "signup_nickname",
    "gender", "user_id",
)
_cfg = {"tables": {}, "fields": {}}


_REQUIRED_TABLES = ("activity_table_id", "group_select_table",
                    "group_result_table", "signup_table_id")


def _require_configured():
    """业务入口的配置闸门：表 ID 与栏位名必须齐全，缺哪几项直接点名。

    为什么不能只看「非空」：configure() 允许分多次注入，若只给 activity_table_id
    就开工，前置检查会放行，真正的 KeyError 要等到业务跑深了才炸，报的还是
    'group_result_table' 这种看不出根因的字样。改成在这里就核对**齐全**，并把
    缺的具体项列出来——Phase 2 接 H5 端点时，一眼就知道漏注了哪个。
    """
    missing = [k for k in _REQUIRED_TABLES if not _cfg["tables"].get(k)]
    missing_fields = [k for k in _REQUIRED_FIELDS if k not in _cfg["fields"]]
    if missing or missing_fields:
        detail = []
        if missing:
            detail.append(f"缺少表 ID：{missing}")
        if missing_fields:
            detail.append(f"缺少栏位名：{missing_fields}")
        raise RuntimeError(
            "lib.grouping 未 configure 完整，不能执行分组业务（"
            + "；".join(detail)
            + "）。表 ID 与栏位名必须由调用端注入——沉默的默认值等于写进错误的 base。")


# 分组状态（写进「分组状态」单选栏位的取值）
STATUS_COLLECTING = "收集中"
STATUS_CLOSED = "已截止"
STATUS_DONE = "已完成"
STATUS_NOT_STARTED = "未开始"
SIGNUP_STATUS_SIGNED = "已报名"
# 「轮次」是单选栏位，飞书侧只配了 1-10。写入更大的值会整批失败并触发回滚，
# 所以只在 preview 里事前挡住；执行路径刻意不拦——bot 端拦了就改了既有行为。
MAX_ROUND = 10

# 分组算法评分（与 JS 版一致）。这是算法的一部分而不是环境配置，两端永远相同，
# 所以住在 lib 里，bot/constants.py 不再另存一份。
PRIORITY_SCORES = {1: 100, 2: 90, 3: 80, 4: 70, 5: 65, 6: 60, 7: 55, 8: 50, 9: 45, 10: 40}
MATCHMAKER_STAR_SCORES = {5: 100, 4: 90, 3: 80, 2: 75, 1: 70}
WEIGHT_USER_SELECTION = 0.7
WEIGHT_MATCHMAKER_PICK = 0.3


# ---------------------------------------------------------------- 分组算法

def _dup_nicknames(participants):
    """参与者昵称是否存在重复（决定下拉选项是否需要追加编号消歧）"""
    names = [p.get("nickname", "") for p in participants]
    return len(names) != len(set(names))


def _option_label(nickname, user_id, with_uid):
    """下拉选项文案：有重名时统一追加编号，避免选错人；缺编号时退回纯昵称"""
    if with_uid and user_id:
        return f"{nickname}（{user_id}）"
    return nickname


def _user_selection_score(priority):
    return PRIORITY_SCORES.get(priority, 0)




def _mutual_affinity_score(p1_id, p2_id, selections, matchmaker_picks=None):
    """计算互有好感分数（双向分数相加，含权重）—— 严格对照JS版"""
    if matchmaker_picks is None:
        matchmaker_picks = []

    # p1 -> p2
    us1 = 0
    if p1_id in selections:
        for s in selections[p1_id]:
            if s["id"] == p2_id:
                us1 = _user_selection_score(s["priority"])
                break
    # p2 -> p1
    us2 = 0
    if p2_id in selections:
        for s in selections[p2_id]:
            if s["id"] == p1_id:
                us2 = _user_selection_score(s["priority"])
                break

    # 红娘推荐分数
    ms1 = ms2 = 0
    for pick in matchmaker_picks:
        if pick.get("person1_id") == p1_id and pick.get("person2_id") == p2_id:
            ms1 = max(ms1, MATCHMAKER_STAR_SCORES.get(pick.get("stars", 0), 0))
        if pick.get("person1_id") == p2_id and pick.get("person2_id") == p1_id:
            ms2 = max(ms2, MATCHMAKER_STAR_SCORES.get(pick.get("stars", 0), 0))

    final1 = us1 * WEIGHT_USER_SELECTION + ms1 * WEIGHT_MATCHMAKER_PICK
    final2 = us2 * WEIGHT_USER_SELECTION + ms2 * WEIGHT_MATCHMAKER_PICK
    return final1 + final2




def _group_affinity_score(candidate_id, group_member_ids, selections, matchmaker_picks=None):
    """候选人与小组所有成员的亲和力总分"""
    total = 0
    for mid in group_member_ids:
        total += _mutual_affinity_score(candidate_id, mid, selections, matchmaker_picks)
    return total




def _create_pair_scores_table(males, females, selections, matchmaker_picks=None):
    """创建所有男女配对的分数表，按分数降序"""
    table = []
    for m in males:
        for f in females:
            table.append({
                "maleId": m, "femaleId": f,
                "score": _mutual_affinity_score(m, f, selections, matchmaker_picks)
            })
    table.sort(key=lambda x: x["score"], reverse=True)
    return table




def _find_core_pair(pair_scores, assigned):
    """找第一个双方都未分配的配对"""
    for pair in pair_scores:
        if pair["maleId"] not in assigned and pair["femaleId"] not in assigned:
            return pair
    return None




def _add_unassigned_to_group(group, males, females, assigned):
    """核心配对找不到时，各加一个未分配的男女"""
    um = [m for m in males if m not in assigned]
    uf = [f for f in females if f not in assigned]
    if um:
        group["male_ids"].append(um[0])
        assigned.add(um[0])
    if uf:
        group["female_ids"].append(uf[0])
        assigned.add(uf[0])




def _determine_target_gender(group, target_male, target_female):
    """确定需要添加的性别"""
    if len(group["male_ids"]) < target_male and len(group["female_ids"]) < target_female:
        return "male" if len(group["male_ids"]) <= len(group["female_ids"]) else "female"
    elif len(group["male_ids"]) < target_male:
        return "male"
    elif len(group["female_ids"]) < target_female:
        return "female"
    return None




def _expand_group_to_full(group, males, females, assigned, target_male, target_female,
                          selections, matchmaker_picks=None):
    """扩充小组到满编"""
    max_iter = max(target_male, target_female) * 2
    for _ in range(max_iter):
        if len(group["male_ids"]) >= target_male and len(group["female_ids"]) >= target_female:
            break
        gender = _determine_target_gender(group, target_male, target_female)
        if not gender:
            break
        pool = [x for x in (males if gender == "male" else females) if x not in assigned]
        if not pool:
            break
        members = group["male_ids"] + group["female_ids"]
        scored = [{"id": c, "score": _group_affinity_score(c, members, selections, matchmaker_picks)}
                  for c in pool]
        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]["id"]
        if gender == "male":
            group["male_ids"].append(best)
        else:
            group["female_ids"].append(best)
        assigned.add(best)




def _distribute_remaining(groups, all_remaining, males_set, females_set, full_count):
    """剩余人员平均分配到各满编组"""
    if not all_remaining or full_count == 0:
        return
    rem_males = [x for x in all_remaining if x in males_set]
    rem_females = [x for x in all_remaining if x in females_set]
    m_per = len(rem_males) // full_count
    m_extra = len(rem_males) % full_count
    f_per = len(rem_females) // full_count
    f_extra = len(rem_females) % full_count
    mi = fi = 0
    for gi in range(full_count):
        g = groups[gi]
        mc = m_per + (1 if gi < m_extra else 0)
        fc = f_per + (1 if gi < f_extra else 0)
        for _ in range(mc):
            if mi < len(rem_males):
                g["male_ids"].append(rem_males[mi])
                mi += 1
        for _ in range(fc):
            if fi < len(rem_females):
                g["female_ids"].append(rem_females[fi])
                fi += 1




def run_grouping_algorithm(participants, selections, males_per_group, females_per_group,
                           matchmaker_picks=None):
    """
    卫星滚动分组法 —— 严格对照JS版generateGroups
    participants: [{"id": "xxx", "gender": "male"/"female"}, ...]
    selections: {user_id: [{"id": target_id, "priority": 1-7}, ...], ...}
    matchmaker_picks: [] (红娘推荐，暂不使用)
    返回: [{"group_id": 1, "male_ids": [...], "female_ids": [...]}, ...]
    """
    if matchmaker_picks is None:
        matchmaker_picks = []

    # 参数校验
    if males_per_group < 1 or females_per_group < 1:
        raise ValueError("每组男女人数必须大于0")

    males = [p["id"] for p in participants if p.get("gender") == "male"]
    females = [p["id"] for p in participants if p.get("gender") == "female"]
    males_set = set(males)
    females_set = set(females)

    max_by_males = len(males) // males_per_group
    max_by_females = len(females) // females_per_group
    full_count = min(max_by_males, max_by_females)

    if full_count == 0:
        return []

    pair_scores = _create_pair_scores_table(males, females, selections, matchmaker_picks)
    assigned = set()
    groups = []

    for gi in range(full_count):
        group = {"group_id": gi + 1, "male_ids": [], "female_ids": []}
        core = _find_core_pair(pair_scores, assigned)
        if not core:
            _add_unassigned_to_group(group, males, females, assigned)
        else:
            group["male_ids"].append(core["maleId"])
            group["female_ids"].append(core["femaleId"])
            assigned.add(core["maleId"])
            assigned.add(core["femaleId"])

        _expand_group_to_full(group, males, females, assigned, males_per_group,
                              females_per_group, selections, matchmaker_picks)

        if len(group["male_ids"]) >= males_per_group and len(group["female_ids"]) >= females_per_group:
            groups.append(group)
        else:
            # 不满编，退回人员，停止创建
            for mid in group["male_ids"] + group["female_ids"]:
                assigned.discard(mid)
            break

    # 剩余人员
    remaining = [x for x in males if x not in assigned] + [x for x in females if x not in assigned]
    if remaining and len(groups) > 0:
        _distribute_remaining(groups, remaining, males_set, females_set, len(groups))

    return groups


# ---------------------------------------------------------------- 内部读取

def _rows_of(client, table_key, conditions):
    """按条件查表。读类路径一律把「读不到」当空表：这些数据只是给管理员看的
    计数与名单，飞书抖一下不该让整个指令失败——bot 侧原本就是这么兜的。"""
    return client.search_records(_cfg["tables"][table_key], {
        "conjunction": "and", "conditions": conditions}) or []


def _selection_rows(client, activity_id):
    """该活动的志愿记录（读类路径统一把「读不到」当空表）。"""
    return _rows_of(client, "group_select_table", [
        {"field_name": _cfg["fields"]["gs_activity_id"], "operator": "is", "value": [activity_id]}])


def _scan_selections(records, activity_id=None):
    """单遍扫描志愿记录，产出管理台计数与算法输入（**去重口径的统一实现**）。

    返回 (submitted_oids, participants, selections, skipped, by_oid)：
    - submitted_oids：非空 open_id 去重列表（首次出现顺序）。「已提交 N 人」与
      「未提交名单」都从它派生，同一人多条记录只算一次；
    - participants / selections：同人取**首条性别有效**的记录；性别缺失的行不占
      去重位——首行性别缺失、次行有效时人仍参与分组（H5 旧实现会把人整个吞掉，
      与算法输入对不上，这是刻意修正）；
    - skipped：性别无效且尚未计入 participants 的行数（沿用旧算法口径）；
    - by_oid：open_id -> 首条有效性别记录的 fields（名单展示用）。
    """
    f = _cfg["fields"]
    submitted_oids = []
    submitted_set = set()
    participants = []
    selections = {}
    skipped = 0
    by_oid = {}
    for sr in records:
        sf = sr.get("fields", {})
        if activity_id is not None and get_field_text(sf, f["gs_activity_id"]) != activity_id:
            continue
        oid = get_field_text(sf, f["gs_selector_oid"])
        if not oid:
            continue
        if oid not in submitted_set:
            submitted_set.add(oid)
            submitted_oids.append(oid)
        if oid in by_oid:
            continue
        gender = get_field_text(sf, f["gs_selector_gender"])
        if gender not in ("男性", "女性"):
            skipped += 1
            continue
        by_oid[oid] = sf
        participants.append({"id": oid, "gender": "male" if gender == "男性" else "female"})
        choices = []
        for i, cf in enumerate(f["gs_choices"]):
            val = get_field_text(sf, cf)
            if val:
                choices.append({"id": val, "priority": i + 1})
        if choices:
            selections[oid] = choices
    return submitted_oids, participants, selections, skipped, by_oid


def summarize_selections(records, activity_id=None):
    """管理台口径的志愿汇总（**去重是刻意语义，不是重构产物**）。

    `fetch_status`（bot「分组状态」）、`preview_round` 与 H5 管理台端点统一走这里。
    旧实现按记录**行数**计数：同一人有重复志愿行时，bot「已提交选择：N人」会和
    「查看未提交」的去重口径对不上（两个数字相加超过报名总数）。本函数按非空
    open_id 去重，只在脏数据（重复行/空 oid）下数字才与旧版不同。

    records：原始记录列表（飞书 search 结果或 H5 快照均同构）；
    activity_id：非空时只统计该活动的记录。
    """
    _require_configured()
    submitted_oids, participants, _selections, skipped, _by_oid = _scan_selections(
        records, activity_id)
    n_males = sum(1 for p in participants if p["gender"] == "male")
    return {
        "submitted_count": len(submitted_oids),
        "submitted_oids": submitted_oids,
        "male_count": n_males,
        "female_count": len(participants) - n_males,
        "skipped": skipped,
    }


def _collect_selections(client, activity_id):
    """把该活动的志愿记录收成算法要的形状。

    回传 (参与者, 选择表, 因性别缺失被跳过的人数)；去重规则全在 _scan_selections。
    性别不是「男性/女性」的直接跳过：算法只认 male/female，留着只会让人数统计虚高。
    """
    rows = _selection_rows(client, activity_id)
    _submitted, participants, selections, skipped, _by_oid = _scan_selections(rows, activity_id)
    return participants, selections, skipped


def collect_participants(activity_id, *, client, find_user_by_openid):
    """取该活动的报名者：[{open_id, nickname, gender, user_id}]。

    逐人查用户表取性别是串行 API 的耗时瓶颈（大活动尤其明显），用线程池并发查，
    显著降低延迟。性别来源由调用端决定（bot 走 queries 版，H5 走快照版）。
    """
    _require_configured()
    f = _cfg["fields"]
    signups = _rows_of(client, "signup_table_id", [
        {"field_name": f["signup_activity_id"], "operator": "is", "value": [activity_id]},
        {"field_name": f["signup_status"], "operator": "is", "value": [SIGNUP_STATUS_SIGNED]},
    ])

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
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="signup-gender") as pool:
        for u in pool.map(_fetch, signup_rows):
            users.append(u)
    return users


# ---------------------------------------------------------------- 读接口

def fetch_status(activity_id, *, client, activity, find_user_by_openid):
    """分組狀態：管理台要的那格数字。"""
    _require_configured()
    f = _cfg["fields"]
    af = activity.get("fields", {})
    signups = collect_participants(activity_id, client=client, find_user_by_openid=find_user_by_openid)
    summary = summarize_selections(_selection_rows(client, activity_id), activity_id)
    return {
        "activity_id": activity_id,
        "name": get_field_text(af, f["activity_name"]),
        "group_status": get_field_text(af, f["group_status"]) or STATUS_NOT_STARTED,
        "males_per_group": get_field_number(af, f["male_per_group"], 0),
        "females_per_group": get_field_number(af, f["female_per_group"], 0),
        "signup_count": len(signups),
        # 去重非空 open_id 数，与 fetch_unsubmitted 同口径（刻意修正旧的「行数」口径）
        "submitted_count": summary["submitted_count"],
    }


def fetch_unsubmitted(activity_id, *, client, activity, find_user_by_openid):
    """未提交志愿的报名者名单。"""
    _require_configured()
    f = _cfg["fields"]
    act_name = get_field_text(activity.get("fields", {}), f["activity_name"])
    signups = collect_participants(activity_id, client=client, find_user_by_openid=find_user_by_openid)
    summary = summarize_selections(_selection_rows(client, activity_id), activity_id)
    submitted_oids = set(summary["submitted_oids"])

    unsubmitted = []
    for s in signups:
        oid = s["open_id"]
        if oid in submitted_oids:
            continue
        user_id = ""
        for rec in find_user_by_openid(oid):
            user_id = rec.get("fields", {}).get(f["user_id"], "") or ""
        unsubmitted.append({
            "open_id": oid, "user_id": user_id,
            "gender": s["gender"], "nickname": s["nickname"],
        })

    return {
        "activity_id": activity_id,
        "name": act_name,
        "total": len(unsubmitted),
        "unsubmitted": unsubmitted,
        "all_submitted": not unsubmitted,
    }


def preview_round(activity_id, round_no, *, client, activity, find_user_by_openid):
    """执行前的影响面预览——**一个栏位都不写**。

    绝不碰「分组状态」：执行路径一进门就把活动改成「已截止」，preview 若沿用同
    一条路径，管理员只是「看一眼」就把活动截止掉，用户立刻不能提交志愿。
    所以这里是独立的一条纯读路径，与 execute_round 没有共用写入代码。
    """
    _require_configured()
    f = _cfg["fields"]
    af = activity.get("fields", {})
    m_per = int(get_field_number(af, f["male_per_group"], 0))
    f_per = int(get_field_number(af, f["female_per_group"], 0))

    summary = summarize_selections(_selection_rows(client, activity_id), activity_id)
    n_males = summary["male_count"]
    n_females = summary["female_count"]
    skipped = summary["skipped"]
    expected = min(n_males // m_per, n_females // f_per) if m_per > 0 and f_per > 0 else 0

    all_results = _rows_of(client, "group_result_table", [
        {"field_name": f["gr_activity_id"], "operator": "is", "value": [activity_id]}])
    rounds = {get_field_text(r.get("fields", {}), f["gr_round"]) for r in all_results}
    rounds.discard("")
    existing_rounds = sorted(rounds, key=lambda s: int(s) if s.isdigit() else 0)
    this_round = [r for r in all_results
                  if get_field_text(r.get("fields", {}), f["gr_round"]) == str(round_no)]

    blockers = []
    if m_per < 1 or f_per < 1:
        blockers.append(f"每组男女人数必须大于0（当前：{m_per}男{f_per}女），请先设置再截止")
    if m_per > 0 and f_per > 0 and (n_males < m_per or n_females < f_per):
        blockers.append(f"人数不足：当前 {n_males}男{n_females}女，"
                        f"每组需要 {m_per}男{f_per}女（至少 {m_per}男{f_per}女）")
    if this_round:
        blockers.append(f"第{round_no}轮已生成分组结果，请勿重复执行")
    if not 1 <= round_no <= MAX_ROUND:
        blockers.append(f"第{round_no}轮超出单选字段上限（轮次字段只支持 1-{MAX_ROUND}）")

    return {
        "activity_id": activity_id,
        "name": get_field_text(af, f["activity_name"]),
        "group_status": get_field_text(af, f["group_status"]) or STATUS_NOT_STARTED,
        "round": round_no,
        "males_per_group": m_per,
        "females_per_group": f_per,
        "male_count": n_males,
        "female_count": n_females,
        "participant_count": n_males + n_females,
        "skipped_no_gender": skipped,
        "expected_groups": expected,
        "existing_rounds": existing_rounds,
        "existing_round_results": len(this_round),
        "historical_results_to_clear": len(all_results),
        "historical_rounds_to_clear": existing_rounds,
        # 加法字段：clear_results=False（开启新一轮）时保留的轮次与结果条数。
        # 旧字段语义不动——它们始终回答「会删掉什么」，这两个回答「会留下什么」。
        "rounds_preserved": existing_rounds,
        "results_preserved": len(all_results),
        "can_execute": not blockers,
        "blockers": blockers,
    }


def fetch_result(activity_id, round_no, *, client, activity):
    """读取某活动某轮次的分组结果（与 execute_round 回传的 groups 同构）。

    `round_no=None` 时取最新轮次。结果表由 bot 与 H5 共同写入，H5 端必须用直读
    client（_grouping_client，实时读飞书），不吃快照——轮次切换要立刻可见。

    返回 {"activity_id", "name", "round", "rounds", "groups"}；对应轮次没有
    记录时 groups=[]（round 为 None 或调用方指定的轮次），由调用端决定怎么呈现。
    """
    _require_configured()
    f = _cfg["fields"]
    af = (activity or {}).get("fields", {})
    rows = _rows_of(client, "group_result_table", [
        {"field_name": f["gr_activity_id"], "operator": "is", "value": [activity_id]}])

    rounds = sorted({int(rv) for rv in
                     (get_field_text(r.get("fields", {}), f["gr_round"]) for r in rows)
                     if rv.strip().isdigit()})
    wanted = int(round_no) if round_no is not None else (rounds[-1] if rounds else None)

    by_group = {}
    if wanted is not None:
        for r in rows:
            sf = r.get("fields", {})
            if get_field_text(sf, f["gr_round"]) != str(wanted):
                continue
            oid = get_field_text(sf, f["gr_user_oid"])
            if not oid:
                continue
            group_no = int(get_field_number(sf, f["gr_group_no"], 0))
            by_group.setdefault(group_no, []).append({
                "open_id": oid,
                "gender": get_field_text(sf, f["gr_user_gender"]),
                # 写入栏位缺昵称时退回 open_id（与 execute_round 的展示口径一致）
                "display_name": get_field_text(sf, f["gr_user_name"]) or oid,
            })

    groups = []
    for group_no in sorted(by_group):
        members = by_group[group_no]
        # 与 execute_round 的成员顺序一致：男先女后；同性别保持结果表里的原始顺序
        members.sort(key=lambda m: 0 if m["gender"] == "男性" else (1 if m["gender"] == "女性" else 2))
        groups.append({"group_id": group_no, "members": members})

    return {
        "activity_id": activity_id,
        "name": get_field_text(af, f["activity_name"]),
        "round": wanted,
        "rounds": rounds,
        "groups": groups,
    }


# ---------------------------------------------------------------- 写接口

def start_group(activity_id, males_per_group, females_per_group, *, client, activity,
                treat_search_failure_as_empty=True, clear_results=True):
    """开放志愿收集；默认同时清空该活动全部轮次的历史分组结果。

    `activity` 由调用端在锁外先解析好——H5 从快照拿 record_id，bot 直接用
    find_activity_by_id，两者都别把一次网络读压在锁里。

    **调用方必须已持有 grouping_lock(activity_id)。**

    `clear_results`：
    - True（默认，bot「开始填志愿」与 H5「重新开放填志愿」走这条）：开放收集
      + 清空全部历史结果，供「重跑既有轮次」；`cleared_results` 是真实清除笔数。
    - False（H5「开启新一轮」）：只写「分组状态=收集中」与每组人数，
      group_result_table **不读也不删**（连 search 都不发，也不会有 SEARCH_FAILED），
      历史轮次全部保留，`cleared_results` 恒为 0。算法是确定性的，直接重跑旧轮次
      毫无意义——保留历史 + 重开收集才能跑出第 N+1 轮。

    `treat_search_failure_as_empty`：清历史时读失败把「读不到」当成「没有历史
    结果」（bot 的老行为），还是直接中止。只在 clear_results=True 时有意义。
    """
    _require_configured()
    f = _cfg["fields"]
    af = activity.get("fields", {})
    activity_name = get_field_text(af, f["activity_name"])
    record_id = activity.get("record_id")

    client.update_record(_cfg["tables"]["activity_table_id"], record_id, {
        f["group_status"]: STATUS_COLLECTING,
        f["male_per_group"]: males_per_group,
        f["female_per_group"]: females_per_group,
    })

    if not clear_results:
        return {
            "code": "OK",
            "activity_id": activity_id,
            "activity_name": activity_name,
            "males_per_group": males_per_group,
            "females_per_group": females_per_group,
            "cleared_results": 0,
        }

    # 重新开放志愿收集：清除该活动历史分组结果，否则「执行分组」的幂等保护会拦截重跑同一轮次
    old_results = client.search_records(_cfg["tables"]["group_result_table"], {
        "conjunction": "and",
        "conditions": [{"field_name": f["gr_activity_id"], "operator": "is", "value": [activity_id]}],
    })
    if old_results is None and not treat_search_failure_as_empty:
        return {"code": "SEARCH_FAILED", "activity_id": activity_id,
                "activity_name": activity_name}
    if old_results:
        client.batch_delete_records(_cfg["tables"]["group_result_table"],
                                    [r["record_id"] for r in old_results])
        log(f"重新开放志愿收集，已清除 {len(old_results)} 条历史分组结果: {activity_name}")

    return {
        "code": "OK",
        "activity_id": activity_id,
        "activity_name": activity_name,
        "males_per_group": males_per_group,
        "females_per_group": females_per_group,
        "cleared_results": len(old_results or []),
    }


def execute_round(activity_id, round_no, *, client, activity, find_user_by_openid,
                  treat_search_failure_as_empty=True):
    """幂等检查 + 跑算法 + 写结果。

    **调用方必须已持有 grouping_lock(activity_id)。** 锁刻意留在外层：bot 的
    handle_admin_stop_group 在临界区里有两个 return，把锁挪进来就得重排那两个
    分支——重排就是重排控制流，文案不变也保不住。

    `treat_search_failure_as_empty` 同 start_group：幂等检查读不到飞书时，
    bot 当空表继续（老行为），H5 宁可中止——把「读不到」当成「没跑过」去重跑，
    等于用一次限流换掉一份既有结果。
    """
    _require_configured()
    f = _cfg["fields"]
    t = _cfg["tables"]
    af = activity.get("fields", {})
    activity_name = get_field_text(af, f["activity_name"])
    record_id = activity.get("record_id")
    m_per = int(get_field_number(af, f["male_per_group"], 3))
    f_per = int(get_field_number(af, f["female_per_group"], 3))
    # 后续所有读写都用活动记录自己的 ID，不退回入参：入参可能是用户随手敲的别名
    rec_activity_id = get_field_text(af, f["activity_id"]) or ""
    base = {"activity_id": activity_id, "round": round_no, "activity_name": activity_name,
            "males_per_group": m_per, "females_per_group": f_per}

    # 幂等保护：该活动本轮已生成过结果则拒绝重跑（多轮分组可轮次递进），
    # 防止并发/重复执行互相删除覆盖分组结果。
    existing_results = client.search_records(t["group_result_table"], {
        "conjunction": "and",
        "conditions": [
            {"field_name": f["gr_activity_id"], "operator": "is", "value": [activity_id]},
            {"field_name": f["gr_round"], "operator": "is", "value": [str(round_no)]},
        ],
    })
    if existing_results is None and not treat_search_failure_as_empty:
        return dict(base, code="SEARCH_FAILED")
    if existing_results:
        return dict(base, code="ALREADY_DONE")

    # 更新状态为已截止
    client.update_record(t["activity_table_id"], record_id, {f["group_status"]: STATUS_CLOSED})

    participants, selections, skipped = _collect_selections(client, rec_activity_id)
    n_males = sum(1 for p in participants if p["gender"] == "male")
    n_females = sum(1 for p in participants if p["gender"] == "female")

    if n_males < m_per or n_females < f_per:
        # 人数不足，恢复状态为收集中
        client.update_record(t["activity_table_id"], record_id, {f["group_status"]: STATUS_COLLECTING})
        return dict(base, code="NOT_ENOUGH_PEOPLE", males=n_males, females=n_females, skipped=skipped)

    # 运行算法
    try:
        groups = run_grouping_algorithm(participants, selections, m_per, f_per)
    except Exception as e:
        client.update_record(t["activity_table_id"], record_id, {f["group_status"]: STATUS_COLLECTING})
        return dict(base, code="ALGORITHM_ERROR", error=str(e))

    if not groups:
        client.update_record(t["activity_table_id"], record_id, {f["group_status"]: STATUS_COLLECTING})
        return dict(base, code="EMPTY_GROUPS")

    # 保存结果（用户自查 H5 查询），不再逐人群发通知（现场活动）。
    # 批量写入分组结果，避免逐条 create_record 在大活动下（数百条）拖到好几分钟。
    signups = collect_participants(rec_activity_id, client=client,
                                   find_user_by_openid=find_user_by_openid)
    oid_to_nickname = {p["open_id"]: p["nickname"] for p in signups}
    result_records = []
    for g in groups:
        all_members = _group_members(g)
        for oid, gender in all_members:
            result_records.append({
                f["gr_activity_id"]: rec_activity_id,
                f["gr_group_no"]: g["group_id"],
                f["gr_user_oid"]: oid,
                f["gr_user_name"]: oid_to_nickname.get(oid, ""),
                f["gr_user_gender"]: gender,
                f["gr_round"]: str(round_no),
            })
    created = client.batch_create_records(t["group_result_table"], result_records)
    if created < len(result_records):
        # 写入不完整：回滚已写入的部分结果 + 恢复状态，允许重试（避免「0 条结果却显示已完成」）
        partial = client.search_records(t["group_result_table"], {
            "conjunction": "and",
            "conditions": [
                {"field_name": f["gr_activity_id"], "operator": "is", "value": [rec_activity_id]},
                {"field_name": f["gr_round"], "operator": "is", "value": [str(round_no)]},
            ],
        })
        if partial:
            client.batch_delete_records(t["group_result_table"], [p["record_id"] for p in partial])
        client.update_record(t["activity_table_id"], record_id, {f["group_status"]: STATUS_COLLECTING})
        return dict(base, code="PARTIAL_WRITE", created=created, total=len(result_records))

    # 更新状态为已完成
    client.update_record(t["activity_table_id"], record_id, {f["group_status"]: STATUS_DONE})

    log(f"第{round_no}轮分组完成: {activity_name}, {len(groups)}组, {len(participants)}人")
    return dict(base, code="OK", group_count=len(groups), participant_count=len(participants),
                groups=[{"group_id": g["group_id"],
                         "members": [{"open_id": oid, "gender": gender,
                                      # 昵称为空（或不在报名名单里）时退回 open_id，与 fetch_result 同口径
                                      "display_name": oid_to_nickname.get(oid) or oid}
                                     for oid, gender in _group_members(g)]}
                        for g in groups])


def _group_members(g):
    """[{"male_ids": [...], "female_ids": [...]}] 摊平成 [(open_id, 中文性别)]，男先女后。"""
    return ([(mid, "男性") for mid in g["male_ids"]]
            + [(fid, "女性") for fid in g["female_ids"]])


def set_group_flag(activity_id, enabled, *, client, activity):
    """开启/关闭活动的分组功能（控制 H5「我的分组」入口显不显示）。

    不加分组锁：只写一个与分组执行不相干的栏位，且两边都是「设定成明确值」的盲写，
    后写者胜，不存在读-改-写丢更新。与 commands.py 里既有的开关联动同一口径。
    """
    _require_configured()
    f = _cfg["fields"]
    flag = "是" if enabled else "否"
    af = activity.get("fields", {})
    activity_name = get_field_text(af, f["activity_name"])
    client.update_record(_cfg["tables"]["activity_table_id"], activity.get("record_id"),
                         {f["group_flag"]: flag})
    log(f"管理员设置分组功能开关: {activity_name}({activity_id}) -> {flag}")
    return {"code": "OK", "activity_id": activity_id, "name": activity_name,
            "group_flag": flag, "enabled": enabled}
