# scripts/dev — 一次性/排查脚本

这些脚本仅用于开发调试与数据修复，不进生产主流程。脚本清单以本目录为准；每个脚本自带的用法与守卫见其文件头 docstring。

多数脚本依赖对应服务目录的 `local_config` / `bitable` / `tools`，需在对应服务目录下运行，并把该目录加入 `PYTHONPATH`：

```bash
# bot 相关（依赖 bot/local_config.py 与 bot/tools/）
cd bot && PYTHONPATH=. ./venv/bin/python ../scripts/dev/backfill_gender.py

# H5 相关（依赖 web/backend/local_config.py 与 bitable/config）
cd web/backend && PYTHONPATH=. venv/bin/python ../../scripts/dev/h5_check_fields.py
```

- `tools/` — 游离 AI/接口工具与数据修复小工具（以目录为准），运行方式同上（`bot/` 目录 + `PYTHONPATH=.`）以读取 local_config。
- 造测试数据、模拟管理端操作的脚本（`setup_*`、`seed_*`、`simulate_admin_cmd` 等前缀）注意勿在生产误跑；部分脚本自带更强的环境守卫（见文件头）。

## 生产保护

会写数据/改表结构的脚本（`backfill_*`、`fix_*`、`setup_*`、`seed_*`、`add_*`、`simulate_admin_cmd`、`tools/fix_hearts` 等）带 `_prod_guard`：`YIXIANQIAN_ENV` 缺省按 prod 处理，直接运行会提示并退出。放行方式：

```bash
# 测试服：显式声明 dev（勿用 test——IS_DEV 只认 dev，test 会绕过保护却按生产解析配置）
cd bot && YIXIANQIAN_ENV=dev PYTHONPATH=. ./venv/bin/python ../scripts/dev/setup_test_data.py

# 生产确需运行：YX_DEV_ALLOW=1 显式放行
cd bot && YX_DEV_ALLOW=1 PYTHONPATH=. ./venv/bin/python ../scripts/dev/backfill_gender.py
```

只读检查类脚本（`check_*` / `h5_check_*` / `tools/feishu_api.py`）不受限制。
