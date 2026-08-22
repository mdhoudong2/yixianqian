# 部署与协作（一线牵）

## 架构约定

- 单仓 monorepo：`bot/`（飞书机器人）+ `web/`（H5 前后端）
- **GitHub `main` 分支是唯一真相源**，生产服务器只 `git pull --ff-only`，禁止在服务器上直接改代码
- 服务器：`root@172.245.223.118`，代码目录 `/opt/yixianqian`
- 运行时数据（`data/*.json`、`image_cache/`、`venv/`、`local_config.py`）均被 `.gitignore` 排除，更新代码不会覆盖

## 目录与路径

| 组件 | 服务器路径 | systemd |
|---|---|---|
| 机器人 | `/opt/yixianqian/bot/`（venv、local_config 软链至仓库根） | `yixianqian.service` |
| H5 后端 | `/opt/yixianqian/web/backend/` | `yixianqian-h5.service` |
| H5 前端 | `/opt/yixianqian/web/frontend/`（由 Flask 静态服务） | 同 H5 |
| 共享运行时数据 | `/opt/yixianqian/data/`（`SHARED_DATA_DIR`，bot 与 H5 共用） | — |
| 共享代码 | `lib/`（飞书客户端 / 多维表格 DAO / JSON 存储） | — |
| systemd 模板 | `deploy/` | 修改后需 `cp` 到 `/etc/systemd/system/` 并 `daemon-reload` |
| Nginx 配置 | `deploy/nginx/`（生产 `nantou.love.conf`；测试 `test.app.nantou.love.conf`、`test.nantou.love.conf`） | 服务器 `/etc/nginx/sites-enabled/*` 软链至对应环境仓库文件，改后 `nginx -t && systemctl reload nginx` |

> 域名角色：`app.nantou.love` / `test.app.nantou.love` = H5 应用（登录使用）；
> `nantou.love` / `test.nantou.love` = 公开页（根路径，后端注入环境二维码）。


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
bash deploy.sh --pull-only
```

`deploy.sh [bot|h5|all] [--pull-only]` 会先检查本地有无未提交改动，pull 使用 `--ff-only`，语法检查通过才重启。合并进 main 的每个 commit 都会触发 GitHub Actions 语法检查（`.github/workflows/check.yml`）。

## 服务器手工操作（仅在特殊场景）

```bash
ssh root@172.245.223.118
cd /opt/yixianqian && git pull --ff-only origin main
systemctl restart yixianqian yixianqian-h5
# 日志（systemd 管理，bot 与 H5 均输出到 journald）
journalctl -u yixianqian -n 50 -f
journalctl -u yixianqian-h5 -n 50 -f
```

## 首次部署（新服务器）

1. `git clone` 仓库至 `/opt/yixianqian`
2. 建 `local_config.py`（仓库根）、`web/backend/local_config.py`（含 `SHARED_DATA_DIR`，默认取仓库下 `data/`）
3. `python3 -m venv bot/venv && bot/venv/bin/pip install -r bot/requirements.txt`
4. `python3 -m venv web/backend/venv && web/backend/venv/bin/pip install -r web/backend/requirements.txt`
5. 安装 `deploy/*.service` 至 `/etc/systemd/system/`，`systemctl enable --now` 两服务
6. 配置 deploy key（Settings → Deploy keys，只读即可，服务器仅 pull）

## 注意事项

- `SHARED_DATA_DIR`（默认 `<仓库根>/data/`）：bot 与 H5 共享的运行时 JSON 目录，两边 `local_config.py` 需一致
- `scripts/dev/` 会写数据的脚本默认拒绝在生产执行，需 `YX_DEV_ALLOW=1`（见 `scripts/dev/README.md`）
- nginx 配置以仓库 `deploy/nginx/` 为准：`/etc/nginx/sites-enabled/*` 为软链指向仓库文件，修改后执行 `nginx -t && systemctl reload nginx`
- 已归档：`mdhoudong2/yixianqian-h5` 不再接受提交
