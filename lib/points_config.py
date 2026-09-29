"""运营数值集中处：麦穗的分值 / 价格 / 比例 / 期限，以及点赞的每日上限。

「所有分值、价格、比例、期限放在一个配置里」是需求原文（点赞的每日次数同理：
需求里写的是「数值放进配置」）。放一个 Python 模块
（而不是散在业务代码里）解决的是「改一个数要翻五个文件」；SQLite `config`
表解决的是「改一个数要重新部署」。

**两层的关系**：`SPECS` 里的 default 是代码里的真理，`config` 表只存被管理员
改过的项。读的时候先查表、miss 才用默认值——所以以后调整默认值能直接生效，
不会被「当初把 20 存进了库」钉死。

跨进程可见性靠 30 秒 TTL：bot 改了配置，H5 最多 30 秒后看到。账本本身是
实时的，只有这十几个运营数值会有这个延迟，可以接受——总比每次算额度都去
查一次库强（`reconcile_hearts` 是逐用户循环，那会变成 N 次查询）。
"""
import threading
import time

from lib import points_db

# 管理员改配置后，其他进程多久看到。取 30 秒是「让运营改完刷新就能看见」和
# 「别为十几个常量反复查库」之间的折中。
CACHE_TTL_SECONDS = 30

# key → {default, label, min, max}
# label 会被管理员的 `配置` 指令直接打印出来，写成人话。
SPECS = {
    "invite_reward_female": {
        "default": 20, "label": "邀请女生注册奖励", "min": 0, "max": 10000},
    "invite_reward_male": {
        "default": 15, "label": "邀请男生注册奖励", "min": 0, "max": 10000},
    "invite_confirm_days": {
        "default": 7, "label": "邀请奖励确认期（天）", "min": 0, "max": 365},

    "assist_min": {
        "default": 10, "label": "协助组织单次下限", "min": 0, "max": 10000},
    "assist_max": {
        "default": 50, "label": "协助组织单次上限", "min": 0, "max": 10000},
    "assist_activity_cap": {
        "default": 50, "label": "同一活动协助累计上限", "min": 0, "max": 10000},

    "redeem_real_like": {
        "default": 20, "label": "兑换：额外实名喜欢", "min": 0, "max": 10000},
    "redeem_priority_signup": {
        "default": 30, "label": "兑换：优先报名名额", "min": 0, "max": 10000},
    "redeem_wish": {
        "default": 30, "label": "兑换：心愿名额", "min": 0, "max": 10000},
    "redeem_matchmaker": {
        "default": 50, "label": "兑换：红娘人工推荐", "min": 0, "max": 10000},
    "redeem_fee_discount": {
        "default": 40, "label": "兑换：活动费用减免", "min": 0, "max": 10000},

    "fee_discount_rate": {
        "default": 0.7, "label": "费用减免后自付比例", "min": 0.0, "max": 1.0},
    "priority_ratio": {
        "default": 0.3, "label": "优先名额占活动总名额比例", "min": 0.0, "max": 1.0},
    "priority_cancel_hours": {
        "default": 48, "label": "优先名额可退穗的取消提前量（小时）",
        "min": 0, "max": 8760},

    "matchmaker_deadline_days": {
        "default": 14, "label": "红娘推荐处理期限（天）", "min": 1, "max": 365},
    # 需求里「填条件」的是红娘推荐（≤200 字）；心愿只是指定一个人，没有条件框。
    # 这个键一开始叫 wish_condition_max_len，名字对不上它管的事，已改。
    "matchmaker_condition_max_len": {
        "default": 200, "label": "红娘推荐条件字数上限", "min": 1, "max": 2000},

    # 点赞（和麦穗无关，只是数值同样要管理员能改，就放同一张表）。
    # 口径是「每天最多几个**有效**赞」：取消掉的不占次数，见 lib/praise.daily_left。
    "daily_praise_limit": {
        "default": 10, "label": "每天最多点赞次数", "min": 0, "max": 1000},
}

# at=None 表示「还没读过」。不能用 0.0 当空值：monotonic 在部分平台上从开机
# 算起，开机后 30 秒内的 now-0.0 会小于 TTL，于是第一次读被当成缓存命中，
# 用户改过的配置被静默忽略半分钟。
_cache = {"at": None, "overrides": {}}
_cache_lock = threading.Lock()


def _spec(key):
    spec = SPECS.get(key)
    if spec is None:
        raise KeyError(f"未定义的配置项：{key}")
    return spec


def _cast(raw, default):
    """把库里的文本还原成默认值的类型。"""
    if isinstance(default, bool):
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "是", "开"):
            return True
        if text in ("0", "false", "no", "否", "关"):
            return False
        raise ValueError(f"不是布尔值：{raw!r}")
    if isinstance(default, int):
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"不是整数：{raw!r}") from None
    if isinstance(default, float):
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"不是数字：{raw!r}") from None
    return raw


def _check_range(key, spec, value):
    if value < spec["min"] or value > spec["max"]:
        raise ValueError(
            f"{spec['label']}（{key}）超出范围：{value}，应在 "
            f"{spec['min']} ~ {spec['max']} 之间")


def _read_overrides():
    """读 config 表。库还没建好时按「没有覆盖」处理，不抛。

    import 期间读不到是很正常的状态（bot 刚启动、迁移还没跑），
    这时候拿默认值是对的；为此让整个进程起不来才是不对的。
    """
    try:
        conn = points_db.connection()
        rows = conn.execute("SELECT key, value FROM config").fetchall()
    except Exception:
        return {}
    return {r["key"]: r["value"] for r in rows if r["key"] in SPECS}


def _overrides():
    now = time.monotonic()
    with _cache_lock:
        if _cache["at"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
            return _cache["overrides"]
    fresh = _read_overrides()
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["overrides"] = fresh
    return fresh


def invalidate():
    """本进程立即重读。改完配置调一次，别让管理员对着旧值发呆 30 秒。"""
    with _cache_lock:
        _cache["at"] = None
        _cache["overrides"] = {}


def get(key):
    """取配置值。库里改过就用库里的，否则用默认值。"""
    spec = _spec(key)
    raw = _overrides().get(key)
    if raw is None:
        return spec["default"]
    try:
        return _cast(raw, spec["default"])
    except ValueError:
        # 手工改库改坏了：用默认值继续跑，而不是让对账线程崩在一条配置上。
        return spec["default"]


def is_overridden(key):
    _spec(key)
    return key in _overrides()


def validate(values):
    """跨项约束。单项范围在 SPECS 里管，这里只管相互依赖的那几条。

    返回问题清单（空列表 = 通过）。调用方决定是拒绝写入还是只告警。
    """
    problems = []
    if values.get("assist_min", 0) > values.get("assist_max", 0):
        problems.append("协助组织下限大于上限，没人能加穗")
    if values.get("assist_activity_cap", 0) < values.get("assist_max", 0):
        problems.append("同活动累计上限小于单次上限，单次上限形同虚设")
    if values.get("priority_ratio", 0) <= 0:
        problems.append("优先名额比例为 0，任何人都兑不到优先名额")
    return problems


def get_all():
    """全部配置的当前值（含被覆盖的）。规则说明页直接渲染它。"""
    return {key: get(key) for key in SPECS}


def set_value(key, value, operator_oid=""):
    """改一项配置。值先转换、再校验，最后才落库——不合法的一个字节都不写。"""
    spec = _spec(key)
    try:
        casted = _cast(value, spec["default"])
    except ValueError as e:
        raise ValueError(f"{spec['label']}（{key}）：{e}") from None
    _check_range(key, spec, casted)
    merged = get_all()
    merged[key] = casted
    problems = validate(merged)
    if problems:
        raise ValueError("；".join(problems))
    with points_db.transaction() as conn:
        conn.execute(
            "INSERT INTO config(key, value, updated_at, operator_oid) VALUES(?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at, operator_oid=excluded.operator_oid",
            (key, str(casted), points_db.now_str(), operator_oid))
    invalidate()
    return casted


def reset(key, operator_oid=""):
    """恢复默认值（删掉覆盖行，不是把默认值写进去）。"""
    spec = _spec(key)
    with points_db.transaction() as conn:
        conn.execute("DELETE FROM config WHERE key=?", (key,))
    invalidate()
    return spec["default"]


def describe():
    """管理员看的一行行说明：当前值 + 默认值 + 是否被改过。"""
    out = []
    for key, spec in SPECS.items():
        out.append({
            "key": key,
            "label": spec["label"],
            "value": get(key),
            "default": spec["default"],
            "overridden": is_overridden(key),
            "min": spec["min"],
            "max": spec["max"],
        })
    return out
