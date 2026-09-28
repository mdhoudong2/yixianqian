-- 兑换单补一个 ref_id：兑换项关联的那件事（活动 ID）。
--
-- 001 里把这个值是塞进 params(JSON) 的，够存但不够用：结算活动取消要按
-- 「这场活动的所有优先名额/费用减免单」捞人，心愿要在活动开始时按活动捞，
-- 都变成对 JSON 做 LIKE 或者 json_extract 的全表扫——既慢又没法建索引，
-- 而且写错一个键名不会报错，只会静默捞不到人（活动取消了却退不了穗）。
--
-- 单独成列之后上面那些查询都走 idx_redemptions_ref，params 回归它本来的
-- 用途：只给人看的明细（标题、原价、指定了谁）。
--
-- 没有天然关联对象的兑换项（额外实名喜欢、红娘推荐）ref_id 留空串。

ALTER TABLE redemptions ADD COLUMN ref_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_redemptions_ref ON redemptions(ref_id, item, status);
