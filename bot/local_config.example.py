# 复制本文件为 local_config.py 并填入真实值（local_config.py 已被 .gitignore 排除）。
FEISHU_APP_ID = "cli_xxxx"
FEISHU_APP_SECRET = "xxxx"
ASSISTANT_APP_ID = "cli_xxxx"
ASSISTANT_APP_SECRET = "xxxx"
BASE_TOKEN = "xxxx"
ADMIN_OPEN_IDS = ["ou_xxxx"]
TAVILY_API_KEY = "tvly-xxxx"
MODELSCOPE_API_KEY = "ms-xxxx"
DEEPSEEK_API_KEY = "sk-xxxx"
SHARED_DATA_DIR = "/opt/yixianqian"  # bot 与 H5 共享运行时 JSON 目录（生产路径）

# 表 ID（可选）：默认走 constants.py 里的生产表 ID。
# 测试服：把多维表格「复制」成新 base 后表 ID 会变，取消注释并填入新值即可覆盖。
# USER_TABLE_ID = "tblsecbZZv0thaPe"
# LIKE_TABLE_ID = "tblaciMZHRQH7QBA"
# ACTIVITY_TABLE_ID = "tblHLltReY8xHTfu"
# SIGNUP_TABLE_ID = "tblNVJCnohVaWf8t"
# MATCH_TABLE_ID = "tbl8eu9Y85tQZCu7"
# GROUP_SELECT_TABLE = "tblYo86Vd7dmzRQJ"
# GROUP_RESULT_TABLE = "tbl3xxAYhyTDGWAB"

# H5 前端入口（测试服覆盖为测试域名，卡片/通知链接据此生成）
# H5_BASE_URL = "https://testapp.nantou.love"

# 飞书分享链接（表单）。测试服若用「复制的多维表格」，这些 URL 会变，需重新生成后覆盖。
# REGISTER_FORM_URL = "https://xxx.feishu.cn/share/base/form/xxx"
# LIKE_FORM_URL = "https://xxx.feishu.cn/share/base/form/xxx"

# 观察员注册（管理员批量生成的唯一邀请码 + 独立表单）
# OBSERVER_FORM_URL = "https://xxx.feishu.cn/share/base/form/xxx"
