# 部署与协作（一线牵）

## 架构约定

- 单仓 monorepo：`bot/`（飞书机器人）+ `web/`（H5 前后端）
- **GitHub `main` 分支是唯一真相源**，生产服务器只 `git pull --ff-only`，禁止在服务器上直接改代码
- 服务器：`root@172.245.223.118`，代码目录 `/opt/yixianqian`
- 运行时数据（`yixianqian_*.json`、`image_cache/`、`venv/`、`local_config.py`）均被 `.gitignore` 排除，更新代码不会覆盖

## 目录与路径

| 组件 | 服务器路径 | systemd |
|---|---|---|
| 机器人 | `/opt/yixianqian/bot/`（venv、local_config 软链至仓库根） | `yixianqian.service` |
| H5 后端 | `/opt/yixianqian/web/backend/` | `yixianqian-h5.service` |
| H5 前端 | `/opt/yixianqian/web/frontend/`（由 Flask 静态服务） | 同 H5 |
| systemd 模板 | `deploy/` | 修改后需 `cp` 到 `/etc/systemd/system/` 并 `daemon-reload` |

## 日常开发流程

```bash
git clone https://github.com/mdhoudong2/yixianqian
cd yixianqian
cp bot/local_config.example.py bot/local_config.py            # 填密钥
cp web/backend/local_config.example.py web/backend/local_config.py

git checkout -b feat/xxx
git commit -m "..." && git push origin feat/xxx    # 提 PR 合入 main
bash deploy.sh            # 本地: push → 服务器 pull → py_compile → 重启
# 或已有人 push 过、只需服务器拉取:
bash deploy_server.sh
```

`deploy.sh [bot|h5|all]` 会先检查本地有无未提交改动，pull 使用 `--ff-only`，语法检查通过才重启。

## 服务器手工操作（仅在特殊场景）

```bash
ssh root@172.245.223.118
cd /opt/yixianqian && git pull --ff-only origin main
systemctl restart yixianqian yixianqian-h5
# 日志
tail -f /tmp/yixianqian_bot.log
journalctl -u yixianqian-h5 -n 50 -f
```

## 首次部署（新服务器）

1. `git clone` 仓库至 `/opt/yixianqian`
2. 建 `bot/local_config.py`、`web/backend/local_config.py`（含 `SHARED_DATA_DIR`）
3. `python3 -m venv bot/venv && bot/venv/bin/pip install -r bot/requirements.txt`
4. `python3 -m venv web/backend/venv && web/backend/venv/bin/pip install -r web/backend/requirements.txt`
5. 安装 `deploy/*.service` 至 `/etc/systemd/system/`，`systemctl enable --now` 两服务
6. 配置 deploy key（Settings → Deploy keys，只读即可，服务器仅 pull）

## 注意事项

- `SHARED_DATA_DIR`（默认 `/opt/yixianqian`）：bot 与 H5 共享的运行时 JSON 目录，两边 `local_config.py` 需一致
- `scripts/dev/` 脚本仅调试用，勿在生产执行（见 `scripts/dev/README.md`）
- 已归档：`mdhoudong2/yixianqian-h5` 不再接受提交
