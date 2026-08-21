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
