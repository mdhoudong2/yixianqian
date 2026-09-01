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
# 表 ID 默认值 = 生产环境；测试服可在 local_config.py 覆盖同名变量（getattr 回退默认值）。
# 复制多维表格到新 base 后表 ID 会变，需在 local_config.py 里按新值填。
USER_TABLE_ID = getattr(_lc, "USER_TABLE_ID", "tblsecbZZv0thaPe")
OBSERVER_TABLE_ID = getattr(_lc, "OBSERVER_TABLE_ID", "")  # 村情六处独立表（观察员，非普通用户）
LIKE_TABLE_ID = getattr(_lc, "LIKE_TABLE_ID", "tblaciMZHRQH7QBA")
ACTIVITY_TABLE_ID = getattr(_lc, "ACTIVITY_TABLE_ID", "tblHLltReY8xHTfu")
SIGNUP_TABLE_ID = getattr(_lc, "SIGNUP_TABLE_ID", "tblNVJCnohVaWf8t")
GROUP_SELECT_TABLE = getattr(_lc, "GROUP_SELECT_TABLE", "tblYo86Vd7dmzRQJ")
GROUP_RESULT_TABLE = getattr(_lc, "GROUP_RESULT_TABLE", "tbl3xxAYhyTDGWAB")
REPORT_TABLE_ID = getattr(_lc, "REPORT_TABLE_ID", "tblDj4PMHitAmo4T")
MESSAGE_TABLE_ID = getattr(_lc, "MESSAGE_TABLE_ID", "")  # 留言表（生产表ID待建，测试服在 local_config 覆盖）
SUGGESTION_TABLE_ID = getattr(_lc, "SUGGESTION_TABLE_ID", "tbldZ7aWtCA5V3Cg")  # 意见反馈表

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
F_DRIVING = "是否有车"
F_HOUSE_NOTE_HAVE = "房产状况-有-补充内容"
F_HOUSE_NOTE_NONE = "房产状况-无-补充内容"
F_DRIVING_NOTE_HAVE = "是否有车-有-补充内容"
F_DRIVING_NOTE_NONE = "是否有车-无-补充内容"
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
STATUS_OBSERVER = "村情六处"  # 账号状态值：村情六处（非单身看热闹，仅浏览/留言/反馈/查看活动）
F_REGISTER_TIME = "注册时间"
F_UPDATE_TIME = "资料更新时间"
F_LAST_ACTIVE = "最近活跃"
F_REFERRAL_SOURCE = "你是怎么知道这个App的？"
F_IS_FOR_CHILD = "您替子女注册吗？"

# 留言表字段
F_MSG_TARGET_OID = "目标用户open_id"
F_MSG_AUTHOR_OID = "留言人open_id"
F_MSG_AUTHOR_NICKNAME = "留言人昵称"
F_MSG_AUTHOR_UID = "留言人用户ID"
F_MSG_PARENT_ID = "父留言ID"
F_MSG_CONTENT = "内容"
F_MSG_CREATED_AT = "创建时间"
F_MSG_STATUS = "状态"
F_MSG_REPLY_TO_OID = "被回复人open_id"
F_MSG_REPLY_TO_NICKNAME = "被回复人昵称"
F_MSG_REPLY_TO_UID = "被回复人用户ID"
F_MSG_REPLY_TO_ID = "被回复留言ID"

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
        ("房产状况", F_HOUSE, "select"),
        ("汽车", F_DRIVING, "select"),
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

# 「我的资料-修改资料」可编辑字段（姓名/圣名/生日/家乡 及 用户ID/爱心/账号状态/注册时间 等系统字段不可改；昵称已改为可编辑）
# 每项: (字段常量, 展示名, 类型, 选项列表或 None)；类型: text/number/select/multi/phone
_OPT_TRAITS = [
    "喜欢安静", "比较主动", "非常自律", "做事严谨", "性格沉稳", "积极乐观",
    "有始有终", "喜爱冒险", "猎奇", "渴望成功", "理性待事", "正直", "话痨",
    "善于交际", "容易相处", "有点小幽默", "传统的", "思想前卫", "总是充满热情", "讲究效率",
]
_OPT_HOBBIES = [
    "有氧运动", "听音乐", "看电影", "看书", "手绘", "唱歌", "弹吉他", "弹钢琴",
    "剧本杀", "狼人杀", "王者荣耀", "吃鸡", "密室逃脱", "爱车一族", "旅游爱好者",
    "看小说", "拍照片", "养宠物", "做饭", "寻觅美食", "逛博物馆", "游乐场", "蹦迪",
]
_OPT_SPORTS = [
    "篮球", "足球", "乒乓球", "羽毛球", "网球", "台球", "游泳", "跑步", "爬山",
    "射击", "跳绳", "漂流", "瑜伽", "慢走", "跳舞", "骑行", "攀岩", "蹦床",
    "滑雪", "冲浪", "滑冰", "跆拳道", "蹦极", "极限运动", "潜水",
]
_OPT_MBTI = ["E", "I", "S", "N", "T", "F", "J", "P"]
_OPT_EDUCATION = ["大专以下", "大专", "本科", "硕士", "博士"]
_OPT_INCOME = ["10W以下", "10W - 20W", "20W - 30W", "30W - 50W", "50W以上"]
_OPT_LIVE_WITH_PARENTS = ["独立生活", "与父母住在一起", "根据具体情况而定"]

EDITABLE_FIELDS = [
    (F_NICKNAME, "昵称", "text", None),
    (F_HEIGHT, "身高", "number", None),
    (F_EDUCATION, "学历", "select", _OPT_EDUCATION),
    (F_CITY, "现居/工作城市", "text", None),
    (F_CHURCH, "经常去的教堂", "text", None),
    (F_GROUP, "参加的团体", "text", None),
    (F_CHURCH_LOCATION, "教堂所在市-区", "text", None),
    (F_INDUSTRY, "从事行业", "text", None),
    (F_POSITION, "职位", "text", None),
    (F_INCOME, "年收入", "select", _OPT_INCOME),
    (F_HOUSE, "房产状况", "select", ["有", "无"]),
    (F_DRIVING, "汽车", "select", ["有", "无"]),
    (F_PERSONALITY, "我是怎样的人", "text", None),
    (F_SELF_TRAITS, "性格", "multi", _OPT_TRAITS),
    (F_SELF_HOBBIES, "爱好", "multi", _OPT_HOBBIES),
    (F_SELF_SPORTS, "运动", "multi", _OPT_SPORTS),
    (F_MBTI, "MBTI", "multi", _OPT_MBTI),
    (F_LIVE_WITH_PARENTS, "婚后与父母同住", "select", _OPT_LIVE_WITH_PARENTS),
    (F_FAMILY, "家庭情况", "text", None),
    (F_PARTNER_CRITERIA, "理想的TA", "text", None),
    (F_PARTNER_TRAITS, "TA的性格", "multi", _OPT_TRAITS),
    (F_PARTNER_HOBBIES, "TA的爱好", "multi", _OPT_HOBBIES),
    (F_PARTNER_SPORTS, "TA的运动", "multi", _OPT_SPORTS),
    (F_WECHAT, "微信号", "text", None),
    (F_PHONE, "手机号", "phone", None),
]

# 「修改资料」里需用多行文本框展示的长文本字段（单行会截断、编辑时看不到后面内容）
LONG_TEXT_FIELDS = {F_PERSONALITY, F_FAMILY, F_PARTNER_CRITERIA}

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
F_LIKE_CREATED_AT = "创建时间"
F_LIKE_INITIATOR_GENDER = "发起用户性别"
F_LIKE_TARGET_GENDER = "目标用户性别"

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

# 意见反馈表字段
F_SG_AUTHOR = "反馈人"
F_SG_UID = "用户ID"
F_SG_TYPE = "类型"
F_SG_CONTENT = "内容"
F_SG_CREATED_AT = "提交时间"

# 管理员open_id（敏感，配置于 local_config.py；此处留空回退）
ADMIN_OPEN_IDS = getattr(_lc, "ADMIN_OPEN_IDS", [])

# 爱心配置
INITIAL_HEARTS = 3
MAX_HEARTS = 30

# 服务配置
SERVER_HOST = "0.0.0.0"
SERVER_PORT = getattr(_lc, "SERVER_PORT", 8091)
SESSION_EXPIRE_DAYS = 30

# H5基础URL（测试服在 local_config.py 覆盖为 https://testapp.nantou.love）
H5_BASE_URL = getattr(_lc, "H5_BASE_URL", "https://app.nantou.love")

# 通知存储（机器人与 H5 共享，机器人写入、H5 读取）
NOTIFICATIONS_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_notifications.json")

# 引流埋点存储（public.html 上报，H5 后端记录；与其它运行时 JSON 同放共享数据目录）
TRACK_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_track.json")

# 引流注册来源标识（public.html 直接注册链接预填到"邀请人ID"）
PUBLIC_SOURCE_ID = "PUBLIC"

# public.html「如何注册」弹窗展示的二维码文件名（测试服在 local_config.py 覆盖为 qrcode_test.png）
PUBLIC_QR_CODE = getattr(_lc, "PUBLIC_QR_CODE", "qrcode.png")

# 注册表单链接（H5「邀请好友得爱心」邀请链接，测试服在 local_config.py 覆盖为测试表单）
REGISTER_FORM_URL = getattr(_lc, "REGISTER_FORM_URL", "https://lcnz8zx7fjk4.feishu.cn/share/base/form/shrcnbUryFlARPYl8I60aIA4qAf")
