# scripts/dev — 一次性/排查脚本

这些脚本仅用于开发调试与数据修复，不进生产主流程。多数脚本依赖 `local_config` / `bitable` / `tools`，
需在对应服务目录下运行：

```bash
# bot 相关（依赖 bot/local_config.py 与 bot/tools/）
cd bot && ../venv/bin/python ../scripts/dev/backfill_gender.py

# H5 相关（依赖 web/backend/local_config.py 与 bitable/config）
cd web/backend && venv/bin/python ../../scripts/dev/h5_check_fields.py
```

- `tools/` — 游离 AI 工具（feishu_api / tavily_search / modelscope_vision / fix_hearts），需从 `bot/` 目录运行以读取 local_config。
- `setup_test_*.py` / `simulate_admin_cmd.py` — 造测试数据与模拟管理端操作，注意勿在生产误跑。

## 生产保护

会写数据/改表结构的脚本（backfill_*、fix_*、setup_*、add_*、simulate_admin_cmd、tools/fix_hearts 等）
默认拒绝在生产环境执行，直接运行会提示并退出。确需在生产运行时：

```bash
YX_DEV_ALLOW=1 python3 scripts/dev/backfill_gender.py
```

只读检查类脚本（check_* / h5_check_* / tools/feishu_api.py）不受限制。
