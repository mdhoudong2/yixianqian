# 一线牵 yixianqian (monorepo)

> 机器人(bot) + H5(web) 单仓。原 `yixianqian` 与 `yixianqian-h5` 已合并，`yixianqian-h5` 归档只读。

## 目录
```
yixianqian/
├── bot/                    # 飞书机器人
│   ├── yixianqian_bot_ws.py
│   ├── yixianqian_backend.py
│   ├── local_config.example.py
│   └── tools/              # feishu_api / tavily_search / modelscope_vision / fix_hearts
├── web/
│   ├── backend/            # Flask H5 后端 (app.py, bitable.py, config.py, gunicorn.conf.py)
│   └── frontend/           # 静态 H5 (index.html, public.html, lib/)
├── scripts/
│   ├── dev/                # 一次性/排查脚本 (add_*, backfill_*, check_*, setup_test_*)
│   └── ops/                # 运维脚本 (yixianqian_guard.sh, guard_dev.sh, supervisor.sh)
├── deploy/                 # 部署与 systemd
│   ├── yixianqian.service
│   └── yixianqian-h5.service
├── shared/                 # 抽公共常量(待收敛 BASE_TOKEN/表ID)
├── data/                   # 运行时 JSON / image_cache (gitignore)
├── deploy.sh               # 本地 push → 服务器 pull → 重启
├── deploy_server.sh        # 服务器纯拉取
└── 文案对照表.md
```

## 协作
```bash
# 首次
git clone https://github.com/mdhoudong2/yixianqian && cd yixianqian
cp bot/local_config.example.py bot/local_config.py
cp web/backend/local_config.example.py web/backend/local_config.py  # 填密钥

# 日常
git checkout -b feat/xxx && git commit -m "..." 
git push origin feat/xxx  # 提 PR → 合入 main
bash deploy.sh          # 或 bash deploy_server.sh (已有人 push 时只在服务器拉)
```

服务器只 `git pull --ff-only`，禁止直接改代码。`local_config.py`/`*.json`/`image_cache`/`venv` 均不入库。

## 部署
- `bot`: `bot/venv` + `systemctl restart yixianqian` (WorkingDirectory `/opt/yixianqian/bot` 兼容期提供根目录软链)
- `web`: `web/backend/venv` + `systemctl restart yixianqian-h5` (WorkingDirectory `/opt/yixianqian/web/backend`)

## 归档
`yixianqian-h5` 仓库已归档，README 指向本仓。
