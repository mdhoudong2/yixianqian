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
# 一对一结果表（Phase 2）：先在多维表格建表，再把表 ID 填到这里；
# 留空＝一对一功能不可用（config.py 缺省为空，不补默认值）。两份配置必须同 base。
# 注：志愿表已统一（2026-10-13）——一对一志愿与分组志愿共用 GROUP_SELECT_TABLE，
#     旧 ONETOONE_SELECT_TABLE 配置不再生效（config.ONETOONE_SELECT_TABLE 恒等于 GROUP_SELECT_TABLE）。
# ONETOONE_RESULT_TABLE = "tblxxxx"
# REPORT_TABLE_ID = "tblDj4PMHitAmo4T"

# H5 前端入口（测试服覆盖为测试域名）
# H5_BASE_URL = "https://testapp.nantou.love"
# SERVER_PORT = 8092  # 仅直接 python app.py 调试用；gunicorn 端口由 systemd 的 BIND 环境变量决定

# 照片每日上传上限（默认 10；调整后需重启 H5 服务）
# PHOTO_DAILY_LIMIT = 10

# 測試環境快速登入（僅測試服；生產勿開）。TEST_LOGIN_KEY 必須非空，uid 必須在允許清單內。
# TEST_LOGIN_ENABLED = True
# TEST_LOGIN_KEY = "請填一段隨機長字串"
# TEST_LOGIN_ALLOW_IDS = ["U-0006"]
