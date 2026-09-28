-- 麦穗积分：初始 schema
--
-- 设计要点（改这张表之前先读一遍）：
--
-- 1. 余额不落库。ledger.balance_after 只是**审计快照**，权威余额永远是
--    SUM(ledger.delta)。落一个 balance 列迟早会和流水对不上，而且对不上的
--    那天没人说得清哪个才是对的。
-- 2. 不删除、不修改任何已写入的流水。退还/收回一律新增一条 delta 相反的行，
--    这样任何时点的余额都可以用「把流水从头加一遍」复现。
-- 3. 幂等键是唯一索引、不是「先查后写」。并发下先查后写必然漏（两个进程
--    同时查到「没有」），只有让数据库来拒才真的不重复发。
--    留 NULL 而不是空串：SQLite 的唯一索引把多个 NULL 视为互不相同，
--    这样管理员手动加分的流水（没有天然幂等键）可以有多条。

CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_oid        TEXT    NOT NULL,
    delta           INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    kind            TEXT    NOT NULL,
    reason          TEXT    NOT NULL DEFAULT '',
    ref_type        TEXT    NOT NULL DEFAULT '',
    ref_id          TEXT    NOT NULL DEFAULT '',
    operator_oid    TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    idempotency_key TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_idem ON ledger(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_oid, id);
CREATE INDEX IF NOT EXISTS idx_ledger_ref ON ledger(ref_type, ref_id);

-- 兑换单。兑换与扣分必须在同一个事务里落这两张表，否则会出现
-- 「扣了穗没有单」或「有单没扣穗」，两边都不好补。
--
-- idempotency_key 由兑换项自己构造，天然承担业务约束：
--   redeem:real_like:<oid>:<序号>        每次兑换一个序号
--   redeem:priority:<oid>:<活动ID>       一人一场只能兑一次
--   redeem:wish:<oid>:<活动ID>           同上
--   redeem:matchmaker:<oid>:<序号>       每人同时只能有一张未完成单
--   redeem:fee_discount:<oid>:<活动ID>   一人一场只能兑一次
CREATE TABLE IF NOT EXISTS redemptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_oid        TEXT    NOT NULL,
    item            TEXT    NOT NULL,
    cost            INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    params          TEXT    NOT NULL DEFAULT '{}',
    idempotency_key TEXT    NOT NULL,
    ledger_id       INTEGER,
    refund_ledger_id INTEGER,
    refund_key      TEXT,
    created_at      TEXT    NOT NULL,
    resolved_at     TEXT    NOT NULL DEFAULT '',
    note            TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_redemptions_idem ON redemptions(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_redemptions_refund ON redemptions(refund_key);
CREATE INDEX IF NOT EXISTS idx_redemptions_user ON redemptions(user_oid, id);
CREATE INDEX IF NOT EXISTS idx_redemptions_item ON redemptions(item, status);

-- 邀请关系。生命周期：
--   pending   已用手机号登记，人还没注册（报名时预登记就走这里）
--   confirmed 已注册并填完资料，7 天确认期计时中——**不发穗**
--   finalized 期满且账号正常，已发穗（reward_ledger_id 指向那条流水）
--   clawed_back 期内封禁/注销/虚假，未发或已发已收回
--   rejected  登记时就被判为非新人/自己邀请自己
--
-- 「待确认的麦穗不能兑换」不需要额外拦截：奖励只在 finalized 那一刻才写进
-- ledger，confirmed 期间余额里根本没有它。
CREATE TABLE IF NOT EXISTS invites (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    inviter_oid      TEXT    NOT NULL,
    invitee_phone    TEXT    NOT NULL,
    invitee_oid      TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL,
    gender           TEXT    NOT NULL DEFAULT '',
    source           TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL DEFAULT '',
    bound_at         TEXT    NOT NULL DEFAULT '',
    confirm_due_at   TEXT    NOT NULL DEFAULT '',
    confirmed_at     TEXT    NOT NULL DEFAULT '',
    reward_ledger_id INTEGER,
    note             TEXT    NOT NULL DEFAULT ''
);

-- 一个手机号全库只能有一条邀请记录 —— 这就是「一人只能有一个邀请人」。
-- 放在唯一索引上而不是代码里判，是因为两个邀请人可能同时提交同一个手机号。
CREATE UNIQUE INDEX IF NOT EXISTS idx_invites_phone ON invites(invitee_phone);
-- 同一注册账号也只能被登记一次（手机号可能改，open_id 不会）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_invites_invitee
    ON invites(invitee_oid) WHERE invitee_oid <> '';
CREATE INDEX IF NOT EXISTS idx_invites_inviter ON invites(inviter_oid);
CREATE INDEX IF NOT EXISTS idx_invites_status ON invites(status, confirm_due_at);

-- 新人判定用的历史参与名单（键为手机号）。App 用户表里查不到、这张表里也
-- 查不到，才算「新人」。名单由管理员导入，见 scripts/dev 下的导入脚本。
CREATE TABLE IF NOT EXISTS historical_participants (
    phone    TEXT PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    source   TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);

-- 运行时配置，覆盖 lib/points_config.py 的默认值。
-- 只存被改过的项，没改过的读默认值 —— 这样以后调默认值能直接生效，
-- 不会因为「当初把 20 存进了库」而改不动。
CREATE TABLE IF NOT EXISTS config (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    operator_oid TEXT NOT NULL DEFAULT ''
);
