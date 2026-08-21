"""一线牵机器人 —— 应用配置与多维表格字段常量（模块间共享，勿放业务逻辑）"""
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==================== 应用配置 ====================

# 通过环境变量区分环境：YIXIANQIAN_ENV=dev 为开发版，默认生产版
IS_DEV = os.environ.get("YIXIANQIAN_ENV", "prod") == "dev"

import local_config as _cfg

if IS_DEV:
    APP_ID = _cfg.ASSISTANT_APP_ID
    APP_SECRET = _cfg.ASSISTANT_APP_SECRET
else:
    APP_ID = _cfg.FEISHU_APP_ID
    APP_SECRET = _cfg.FEISHU_APP_SECRET

# 管理员open_id列表（可添加多个）
ADMIN_OPEN_IDS = _cfg.ADMIN_OPEN_IDS

# ==================== 多维表格链接配置（默认生产，测试服在 local_config.py 覆盖） ====================
REGISTER_FORM_URL = getattr(_cfg, "REGISTER_FORM_URL", "https://lcnz8zx7fjk4.feishu.cn/share/base/form/shrcn04AWZCwqilzelLqT5CJsNd")
LIKE_FORM_URL = getattr(_cfg, "LIKE_FORM_URL", "https://lcnz8zx7fjk4.feishu.cn/share/base/form/shrcnNhTZVdSTcmVRzRTwZg0E0b")

# ==================== 多维表格配置 ====================
BASE_TOKEN = _cfg.BASE_TOKEN
# 表 ID 默认值 = 生产环境；测试服在 local_config.py 覆盖同名变量即可（getattr 回退默认值）。
# 复制多维表格到新 base 后表 ID 会变，需在 local_config.py 里按新值填。
USER_TABLE_ID = getattr(_cfg, "USER_TABLE_ID", "tblsecbZZv0thaPe")
LIKE_TABLE_ID = getattr(_cfg, "LIKE_TABLE_ID", "tblaciMZHRQH7QBA")
ACTIVITY_TABLE_ID = getattr(_cfg, "ACTIVITY_TABLE_ID", "tblHLltReY8xHTfu")
SIGNUP_TABLE_ID = getattr(_cfg, "SIGNUP_TABLE_ID", "tblNVJCnohVaWf8t")
MATCH_TABLE_ID = getattr(_cfg, "MATCH_TABLE_ID", "tbl8eu9Y85tQZCu7")

FIELD_NICKNAME = "昵称"
FIELD_FEISHU_ID = "飞书用户ID"
FIELD_HEART_REMAIN = "爱心剩余"
FIELD_ACCOUNT_STATUS = "账号状态"
FIELD_GENDER = "性别"
FIELD_EDUCATION = "学历"
FIELD_SELF_HOBBIES = "我是一个怎样的人-爱好"
FIELD_CREATOR = "创建人"

FIELD_LIKE_INITIATOR = "发起用户昵称"
FIELD_LIKE_TARGET = "目标用户昵称"
FIELD_LIKE_STATUS = "状态"
FIELD_LIKE_HEART_DEDUCTED = "爱心已扣减"
FIELD_LIKE_MESSAGE = "附言"
FIELD_LIKE_INITIATOR_OPENID = "发起用户open_id"
FIELD_LIKE_TARGET_OPENID = "目标用户open_id"
FIELD_LIKE_INITIATOR_ID = "发起用户ID"
FIELD_LIKE_TARGET_ID = "目标用户ID"
FIELD_LIKE_TYPE = "喜欢类型"  # 匿名/实名

FIELD_ACTIVITY_ID = "活动ID"
FIELD_ACTIVITY_NAME = "活动名称"
FIELD_ACTIVITY_CURRENT_SIGNUP = "当前报名人数"
FIELD_ACTIVITY_STATUS = "活动状态"

FIELD_SIGNUP_ACTIVITY_ID = "活动ID"
FIELD_SIGNUP_OPENID = "报名人open_id"
FIELD_SIGNUP_NICKNAME = "报名人昵称"
FIELD_SIGNUP_STATUS = "状态"

FIELD_MATCH_FOR_USER = "推荐给用户"
FIELD_MATCH_TARGET_USER = "被推荐用户"
FIELD_MATCH_REASON = "推荐理由"
FIELD_MATCH_STATUS = "推荐状态"

# 分组功能
GROUP_SELECT_TABLE = getattr(_cfg, "GROUP_SELECT_TABLE", "tblYo86Vd7dmzRQJ")
GROUP_RESULT_TABLE = getattr(_cfg, "GROUP_RESULT_TABLE", "tbl3xxAYhyTDGWAB")
FIELD_GS_ACTIVITY_ID = "活动ID"
FIELD_GS_SELECTOR_OID = "选择人open_id"
FIELD_GS_SELECTOR_NAME = "选择人昵称"
FIELD_GS_SELECTOR_GENDER = "选择人性别"
FIELD_GS_CHOICES = ["第1志愿", "第2志愿", "第3志愿", "第4志愿", "第5志愿", "第6志愿", "第7志愿"]
FIELD_GR_ACTIVITY_ID = "活动ID"
FIELD_GR_GROUP_NO = "组号"
FIELD_GR_USER_OID = "用户open_id"
FIELD_GR_USER_NAME = "用户昵称"
FIELD_GR_USER_GENDER = "用户性别"
FIELD_ACT_GROUP_STATUS = "分组状态"
FIELD_ACT_MALE_PER_GROUP = "每组男生数"
FIELD_ACT_FEMALE_PER_GROUP = "每组女生数"
FIELD_ACT_GROUP_FLAG = "分组功能开启"  # 单选(是/否)：控制 H5 我的页是否显示「我的分组」入口
FIELD_GR_ROUND = "轮次"  # 分组结果轮次，单选(1/2/3...)，支持同活动多次分组

# 邀请功能
FIELD_INVITER_ID = "邀请人ID"  # 邀请人的用户ID（如U-0003）
INITIAL_HEARTS = 3
MAX_HEARTS = 30
# H5 前端入口（卡片/通知链接）。测试服在 local_config.py 覆盖为 https://test.app.nantou.love
H5_BASE_URL = getattr(_cfg, "H5_BASE_URL", "https://app.nantou.love")

# ==================== 本地记录文件 ====================
# bot 与 H5 共享的运行时数据目录（可在 local_config.py 覆盖，默认取仓库下 data/）
SHARED_DATA_DIR = getattr(_cfg, "SHARED_DATA_DIR", None) or os.path.join(_REPO_ROOT, "data")
os.makedirs(SHARED_DATA_DIR, exist_ok=True)
BINDING_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_bindings.json")
NOTIFIED_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_notified.json")  # 记录已发送通知的记录ID，避免重复
WELCOMED_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_welcomed.json")  # 记录已发送进入欢迎消息的用户open_id，避免重复
MENU_CARD_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_menu_card.json")  # 记录上次发送菜单卡片的时间，用于节流
INVITE_REWARDED_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_invites.json")  # 记录已奖励的邀请关系
NOTIFICATIONS_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_notifications.json")  # 共享通知（机器人写，H5读）

WS_HEALTH_CHECK_INTERVAL = 60      # 每60秒检查一次
WS_HEALTH_CHECK_TIMEOUT = 600      # 10分钟无任何事件则强制重连

# 分组算法评分（与 JS 版一致）
PRIORITY_SCORES = {1: 100, 2: 90, 3: 80, 4: 70, 5: 65, 6: 60, 7: 55, 8: 50, 9: 45, 10: 40}
# 红娘星级分数
MATCHMAKER_STAR_SCORES = {5: 100, 4: 90, 3: 80, 2: 75, 1: 70}
# 权重
WEIGHT_USER_SELECTION = 0.7
WEIGHT_MATCHMAKER_PICK = 0.3

def is_admin(open_id):
    return open_id in ADMIN_OPEN_IDS
