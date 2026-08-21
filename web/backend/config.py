# -*- coding: utf-8 -*-
"""一线牵 H5 配置"""

import os

# 仓库根目录（web/backend 的上两级）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 飞书应用配置（生产版，密钥见 local_config.py）
import local_config as _lc
FEISHU_APP_ID = _lc.FEISHU_APP_ID
FEISHU_APP_SECRET = _lc.FEISHU_APP_SECRET
BASE_TOKEN = _lc.BASE_TOKEN
# bot 与 H5 共享的运行时数据目录（可在 local_config.py 覆盖，默认取仓库下 data/）
SHARED_DATA_DIR = getattr(_lc, "SHARED_DATA_DIR", None) or os.path.join(REPO_ROOT, "data")
os.makedirs(SHARED_DATA_DIR, exist_ok=True)

# 多维表格配置
USER_TABLE_ID = "tblsecbZZv0thaPe"
LIKE_TABLE_ID = "tblaciMZHRQH7QBA"
ACTIVITY_TABLE_ID = "tblHLltReY8xHTfu"
SIGNUP_TABLE_ID = "tblNVJCnohVaWf8t"
GROUP_SELECT_TABLE = "tblYo86Vd7dmzRQJ"
GROUP_RESULT_TABLE = "tbl3xxAYhyTDGWAB"
REPORT_TABLE_ID = "tblDj4PMHitAmo4T"

# 用户表字段
F_USER_ID = "用户ID"
F_REAL_NAME = "姓名"
F_NICKNAME = "昵称"
F_FEISHU_ID = "飞书用户ID"
F_GENDER = "性别"
F_BIRTHDAY = "生日"
F_HEIGHT = "身高（cm）"
F_EDUCATION = "学历"
F_BAPTISMAL_NAME = "圣名"
F_CHURCH = "经常去的教堂"
F_CHURCH_LOCATION = "教堂所在市-区"
F_GROUP = "参加的团体"
F_NATIVE_PLACE = "家乡"
F_CITY = "现居/工作城市"
F_INDUSTRY = "从事行业"
F_POSITION = "职位"
F_INCOME = "年收入"
F_HOUSE = "房产状况"
F_DRIVING = "是否拥有汽车"
F_MARRIAGE = "你结过婚吗？"
F_FAMILY = "家庭成员情况"
F_LIVE_WITH_PARENTS = "婚后是否与父母同住"
F_PERSONALITY = "我是一个怎样的人"
F_SELF_TRAITS = "我是一个怎样的人-性格"
F_SELF_HOBBIES = "我是一个怎样的人-爱好"
F_SELF_SPORTS = "我是一个怎样的人-运动"
F_MBTI = "我是个怎样的人-MBTI人格"
F_PARTNER_CRITERIA = "理想中的TA"
F_PARTNER_TRAITS = "理想中的TA-性格"
F_PARTNER_HOBBIES = "理想中的TA-爱好"
F_PARTNER_SPORTS = "理想中的TA-运动"
F_PHOTO = "个人照片"
F_WECHAT = "微信号"
F_PHONE = "手机号"
F_ID_CARD = "身份证号"
F_HEART_REMAIN = "爱心剩余"
F_ACCOUNT_STATUS = "账号状态"
F_REGISTER_TIME = "注册时间"
F_UPDATE_TIME = "资料更新时间"
F_REFERRAL_SOURCE = "你是怎么知道这个App的？"
F_IS_FOR_CHILD = "您替子女注册吗？"

# 简洁行字段：不标注字段名，用「·」连接显示（如 U-0003 · 玛利亚 · 97-10 · 190 · 本科）
SIMPLE_FIELDS = [
    (F_USER_ID, "text"),
    (F_BAPTISMAL_NAME, "text"),
    (F_BIRTHDAY, "birthday"),
    (F_HEIGHT, "number"),
    (F_EDUCATION, "select"),
]

# 牵线卡片「其它字段」分组（与「我的-我的资料」分组一致）
# 每个分组: (分组名, 分组图标, [(展示名, 字段常量, 类型), ...])
# 类型: text/select/number/multi
CARD_SECTIONS = [
    ("基本信息", "👤", [
        ("现居/工作城市", F_CITY, "text"),
        ("家乡", F_NATIVE_PLACE, "text"),
        ("经常去的教堂", F_CHURCH, "text"),
        ("参加的团体", F_GROUP, "text"),
        ("教堂所在市-区", F_CHURCH_LOCATION, "text"),
    ]),
    ("工作与经济", "💼", [
        ("从事行业", F_INDUSTRY, "text"),
        ("职位", F_POSITION, "text"),
        ("年收入", F_INCOME, "select"),
        ("房产状况", F_HOUSE, "text"),
        ("汽车", F_DRIVING, "text"),
    ]),
    ("关于我", "✨", [
        ("我是怎样的人", F_PERSONALITY, "text"),
        ("性格", F_SELF_TRAITS, "multi"),
        ("爱好", F_SELF_HOBBIES, "multi"),
        ("运动", F_SELF_SPORTS, "multi"),
        ("MBTI", F_MBTI, "multi"),
        ("婚后与父母同住", F_LIVE_WITH_PARENTS, "select"),
        ("家庭情况", F_FAMILY, "text"),
    ]),
    ("理想中的TA", "💖", [
        ("理想中的TA", F_PARTNER_CRITERIA, "text"),
        ("性格", F_PARTNER_TRAITS, "multi"),
        ("爱好", F_PARTNER_HOBBIES, "multi"),
        ("运动", F_PARTNER_SPORTS, "multi"),
    ]),
]

# 喜欢关系表字段
F_LIKE_INITIATOR = "发起用户昵称"
F_LIKE_TARGET = "目标用户昵称"
F_LIKE_STATUS = "状态"
F_LIKE_HEART_DEDUCTED = "爱心已扣减"
F_LIKE_MESSAGE = "附言"
F_LIKE_INITIATOR_OPENID = "发起用户open_id"
F_LIKE_TARGET_OPENID = "目标用户open_id"
F_LIKE_INITIATOR_ID = "发起用户ID"
F_LIKE_TARGET_ID = "目标用户ID"
F_LIKE_TYPE = "喜欢类型"

# 活动表字段
F_ACTIVITY_ID = "活动ID"
F_ACTIVITY_NAME = "活动名称"
F_ACTIVITY_DESC = "活动描述"
F_ACTIVITY_LOCATION = "活动地点"
F_ACTIVITY_CONDITION = "参与条件"
F_ACTIVITY_MAX_SIGNUP = "报名人数上限"
F_ACTIVITY_CURRENT_SIGNUP = "当前报名人数"
F_ACTIVITY_FEE = "费用"
F_ACTIVITY_FOOD = "食宿"
F_ACTIVITY_STATUS = "活动状态"
F_ACTIVITY_POSTER = "活动海报"
F_ACTIVITY_GROUP_STATUS = "分组状态"
F_ACTIVITY_MALE_PER_GROUP = "每组男生数"
F_ACTIVITY_FEMALE_PER_GROUP = "每组女生数"
F_ACTIVITY_GROUP_FLAG = "分组功能开启"
F_ACTIVITY_PUBLISH_TIME = "发布时间"
F_ACTIVITY_START_TIME = "开始时间"
F_ACTIVITY_END_TIME = "结束时间"

# 报名表字段
F_SIGNUP_ACTIVITY_ID = "活动ID"
F_SIGNUP_OPENID = "报名人open_id"
F_SIGNUP_NICKNAME = "报名人昵称"
F_SIGNUP_STATUS = "状态"

# 分组选择表字段
F_GS_ACTIVITY_ID = "活动ID"
F_GS_SELECTOR_OID = "选择人open_id"
F_GS_SELECTOR_NAME = "选择人昵称"
F_GS_SELECTOR_GENDER = "选择人性别"
F_GS_CHOICES = ["第1志愿", "第2志愿", "第3志愿", "第4志愿", "第5志愿", "第6志愿", "第7志愿"]

# 分组结果表字段
F_GR_ACTIVITY_ID = "活动ID"
F_GR_GROUP_NO = "组号"
F_GR_USER_OID = "用户open_id"
F_GR_USER_NAME = "用户昵称"
F_GR_USER_GENDER = "用户性别"
F_GR_ROUND = "轮次"

# 举报表字段
F_REPORT_REPORTER = "举报人昵称"
F_REPORT_TARGET = "被举报人昵称"
F_REPORT_REASON = "举报原因"

# 管理员open_id（敏感，配置于 local_config.py；此处留空回退）
ADMIN_OPEN_IDS = getattr(_lc, "ADMIN_OPEN_IDS", [])

# 爱心配置
INITIAL_HEARTS = 3
MAX_HEARTS = 30

# 服务配置
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8091
SESSION_EXPIRE_DAYS = 30

# H5基础URL
H5_BASE_URL = "https://app.nantou.love"

# 通知存储（机器人与 H5 共享，机器人写入、H5 读取）
NOTIFICATIONS_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_notifications.json")

# 引流埋点存储（public.html 上报，H5 后端记录；与其它运行时 JSON 同放共享数据目录）
TRACK_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_track.json")

# 引流注册来源标识（public.html 直接注册链接预填到"邀请人ID"）
PUBLIC_SOURCE_ID = "PUBLIC"
