# 复制本文件为 local_config.py 并填入真实值（local_config.py 已被 .gitignore 排除）。
FEISHU_APP_ID = "cli_xxxx"
FEISHU_APP_SECRET = "xxxx"
BASE_TOKEN = "xxxx"
SHARED_DATA_DIR = "/opt/yixianqian"  # bot 与 H5 共享运行时 JSON 目录（生产路径）
ADMIN_OPEN_IDS = ["ou_xxxx"]  # 管理员飞书 open_id 列表

# 表 ID（可选）：默认走 config.py 里的生产表 ID。
# 测试服：把多维表格「复制」成新 base 后表 ID 会变，取消注释并填入新值即可覆盖。
# USER_TABLE_ID = "tblsecbZZv0thaPe"
# LIKE_TABLE_ID = "tblaciMZHRQH7QBA"
# ACTIVITY_TABLE_ID = "tblHLltReY8xHTfu"
# SIGNUP_TABLE_ID = "tblNVJCnohVaWf8t"
# GROUP_SELECT_TABLE = "tblYo86Vd7dmzRQJ"
# GROUP_RESULT_TABLE = "tbl3xxAYhyTDGWAB"
# REPORT_TABLE_ID = "tblDj4PMHitAmo4T"

# H5 前端入口（测试服覆盖为测试域名）
# H5_BASE_URL = "https://testapp.nantou.love"
# SERVER_PORT = 8092  # 仅直接 python app.py 调试用；gunicorn 端口由 systemd 的 BIND 环境变量决定
