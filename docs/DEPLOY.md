# 部署与协作（一线牵）

## 环境与原则

- 单仓 monorepo：`bot/`（飞书机器人）+ `web/`（H5 前后端）
- **GitHub 仓库是唯一真相源**：服务器只 `git pull --ff-only`，禁止在服务器上直接改代码
- 服务器：`root@172.245.223.118`
- 运行时数据（`data/`、`image_cache/`、`venv/`、`local_config.py`）均被 `.gitignore` 排除，更新代码不会覆盖

两个环境相互独立（代码目录、data 目录、`local_config.py`、systemd 单元、端口）：

| 环境 | 分支 | 代码目录 | systemd 单元 |
|---|---|---|---|
| 生产 | `main` | `/opt/yixianqian` | `yixianqian`（bot）/ `yixianqian-h5` |
| 测试 | `develop` | `/opt/yixianqian-test` | `yixianqian-test`（bot）/ `yixianqian-h5-test` |

端口/绑定以 `gunicorn.conf.py` 与 `deploy/*.service` 为准（生产与测试各用各的）。

## 目录与路径

| 组件 | 路径（两环境同构，`<代码目录>` 见上表） |
|---|---|
| 机器人 | `<代码目录>/bot/`（venv、`local_config.py`） |
| H5 后端 | `<代码目录>/web/backend/`（venv、`local_config.py`） |
| H5 前端 | `<代码目录>/web/frontend/`（由 Flask 静态服务） |
| 共享运行时数据 | `<代码目录>/data/`（`SHARED_DATA_DIR`，bot 与 H5 共用） |
| 共享代码 | `lib/`（飞书客户端 / 多维表格 DAO / 存储 / 共享业务逻辑） |
| systemd 模板 | `deploy/*.service`（修改后 `cp` 到 `/etc/systemd/system/` 并 `daemon-reload`） |
| Nginx 配置 | `deploy/nginx/`（站点配置）；修改后 `nginx -t && systemctl reload nginx` |

- systemd 单元与代码目录一一对应（生产单元跑 `/opt/yixianqian`，测试单元跑 `/opt/yixianqian-test`）——安装/更新单元文件时注意环境。
- Nginx：服务器 `/etc/nginx/sites-enabled/` 中的站点配置引用仓库 `deploy/nginx/` 同名文件（生产/测试主域名为软链，个别站点为同步副本）——**一律以仓库文件为准**；改后 `nginx -t && systemctl reload nginx`。

> 域名角色：`app.nantou.love` / `testapp.nantou.love` = H5 应用（登录使用）；
> `nantou.love` / `test.nantou.love` = 公开页（根路径，后端注入环境二维码）。

## 开发与发布原则

- **提交与验收**：改动先进 `develop` 分支，部署到测试服（`/opt/yixianqian-test`）由用户验收。
- **上生产**：走 `fix/*` 分支——从 `origin/main` 切出，只纳入本次验收通过的改动，提 PR 合入 `main`。**默认禁止 `develop` 直接合入 `main`**（develop 上常混有他人未验收的提交）；仅当机械判定全过（同步点可与 main 树比对、自同步点以来无他人未验收提交、改动范围吻合）才可走「绿色通道」直接提 PR，免移植、免回合并。
- **生产部署**：服务器 `git pull --ff-only origin main` + 重启对应服务（见「服务器手工操作」）。
- **发布收尾**：视情形将 `main` 合回 `develop`；fix 分支与 develop 树相等时免回合并。
- **CI**：`.github/workflows/check.yml`（语法/模板绑定检查、ruff、pytest）在 push / PR 时执行；CI 绿才继续部署。

## deploy.sh 用法

脚本位于仓库根，实际接口：

```
bash deploy.sh [bot|h5|all] [prod|test] [--pull-only] [--yes-prod]
```

- 目标组件默认 `all`；环境默认 `prod`（**生产需显式 `--yes-prod` 防手误**，只发测试服传 `test`）。
- 行为：检查本地无未提交改动 → push 本地对应分支（prod→`main`、test→`develop`）→ 服务器 `git pull --ff-only` → 全量语法检查（H5 另加内联模板检查）→ 重启对应 systemd 单元并做健康检查（bot 等 active；H5 等 `/api/version` 可达）。
- `--pull-only`：跳过本地 push（代码已由他人 push 时），仅让服务器拉取 + 重启。

```bash
bash deploy.sh test              # 测试服：部署 all（develop）
bash deploy.sh h5 --yes-prod     # 生产：仅 H5（显式确认）
bash deploy.sh --pull-only test  # 仅让测试服拉取 + 重启
```

## 服务器手工操作（仅在特殊场景）

```bash
ssh root@172.245.223.118

# 生产
cd /opt/yixianqian && git pull --ff-only origin main
systemctl restart yixianqian yixianqian-h5
# 测试服同理：cd /opt/yixianqian-test && git pull --ff-only origin develop
#             systemctl restart yixianqian-test yixianqian-h5-test

# 日志（systemd 管理，bot 与 H5 均输出到 journald）
journalctl -u yixianqian -n 50 -f          # 生产 bot
journalctl -u yixianqian-h5 -n 50 -f       # 生产 H5
```

## 首次部署（新服务器）

1. `git clone` 仓库至 `/opt/yixianqian`（生产）或 `/opt/yixianqian-test`（测试）
2. 创建两处 `local_config.py`（`bot/`、`web/backend/`，参照各自 `*.example.py` 填写密钥）；两处的 `SHARED_DATA_DIR` 必须一致（默认 `<代码目录>/data/`）
3. `python3 -m venv bot/venv && bot/venv/bin/pip install -r bot/requirements.txt`
4. `python3 -m venv web/backend/venv && web/backend/venv/bin/pip install -r web/backend/requirements.txt`
5. 安装 `deploy/*.service` 到 `/etc/systemd/system/`，`systemctl enable --now` 对应单元
6. 配置 deploy key（Settings → Deploy keys，只读即可，服务器仅 pull）
7. Nginx：以仓库 `deploy/nginx/` 为准接入站点，`nginx -t && systemctl reload nginx`

## 注意事项

- `SHARED_DATA_DIR`：bot 与 H5 共享的运行时 JSON 与积分账本目录；两处 `local_config.py` 必须一致（默认 `<代码目录>/data/`）
- `scripts/dev/` 会写数据的脚本带生产保护：`YIXIANQIAN_ENV` 缺省按 prod 处理，默认拒绝执行；测试服放行用 `YIXIANQIAN_ENV=dev`（注意：`IS_DEV` 仅认 `dev`，写成 `test` 会绕过保护却按生产解析配置），生产确需运行用 `YX_DEV_ALLOW=1`（见 `scripts/dev/README.md`）
- nginx 配置以仓库 `deploy/nginx/` 为准，修改后执行 `nginx -t && systemctl reload nginx`
- 已归档：`mdhoudong2/yixianqian-h5` 不再接受提交
