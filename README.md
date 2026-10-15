# 一线牵

面向教会青年的婚恋交友平台 —— 飞书机器人 + H5 双端，业务数据存于飞书多维表格（Bitable）。

> 生产环境：<https://app.nantou.love> ｜ 机器人：飞书搜索「一线牵」

## 功能概述

按模块划分；具体机制、数值与文案以代码为准。

**飞书机器人（bot/）**
- 注册与审核：表单引导、人工审核、H5 开通
- 牵线通知：喜欢 / 相互喜欢
- 活动：报名与活动通知
- 匹配：分组匹配与一对一匹配（一对一为「女生坐镇、男生上门」）
- 积分奖励
- 主菜单卡片与消息通知汇总

**H5 网页（web/）**
- 牵线：浏览与表达心意
- 消息：喜欢 / 相互喜欢与通知汇总
- 活动：报名、签到、志愿与结果
- 我的：资料查看与修改
- 公开引流页 `public.html`
- 管理台（管理员功能，按权限开放）

## 架构

```
飞书用户 ──消息/卡片──▶ 飞书开放平台 ──WS 长连接──▶ bot/yixianqian_bot_ws.py
用户浏览器 ──HTTP──▶ Nginx ──▶ Flask (web/backend)     │
                                │  读/写           写/读│
                                ▼                     ▼
                           多维表格 Bitable ◀──────────┘
                               ▲
                    运行时共享数据（通知等，SHARED_DATA_DIR）
```

- **数据层**：业务数据存飞书多维表格；积分账本为共享目录下的 SQLite（见 `lib/points_db.py`）
- **bot ↔ H5 通信**：通过共享运行时数据目录（`SHARED_DATA_DIR`，机器人写、H5 读）
- **H5 读取**：本地快照缓存＋定时刷新（策略见 `web/backend/app.py` 与 `docs/读取一致性.md`）
- **外围工具**：图像识别、联网检索、飞书 API 调用等游离工具位于 `scripts/dev/tools/`（以目录为准）

## 技术栈

| 端 | 技术 |
|---|---|
| 机器人 | Python 3.12 · lark-oapi（WS 长连接） |
| H5 后端 | Flask · gunicorn · Pillow · itsdangerous |
| H5 前端 | Vue 3（CDN）· Vant 移动端组件 |
| 数据 | 飞书多维表格 Bitable（积分账本 SQLite） |
| 工具 | ModelScope Vision · Tavily（见 `scripts/dev/tools/`） |

## 快速开始（本地开发）

```bash
git clone https://github.com/mdhoudong2/yixianqian && cd yixianqian

# 1. 配置密钥（两处 local_config.py 的 SHARED_DATA_DIR 需一致，默认 <仓库根>/data/）
cp bot/local_config.example.py bot/local_config.py
cp web/backend/local_config.example.py web/backend/local_config.py

# 2. 机器人
python3 -m venv bot/venv && bot/venv/bin/pip install -r bot/requirements.txt
cd bot && ./venv/bin/python yixianqian_bot_ws.py

# 3. H5 后端（默认 127.0.0.1:8091，可用 BIND 覆盖，见 gunicorn.conf.py）
python3 -m venv web/backend/venv && web/backend/venv/bin/pip install -r web/backend/requirements.txt
cd web/backend && ./venv/bin/gunicorn -c gunicorn.conf.py app:app
```

## 文档

- [部署与协作](docs/DEPLOY.md) —— 环境与部署、发布流程、服务器操作
- [跨进程读取一致性](docs/读取一致性.md) —— H5 快照与跨进程读取规则
- [scripts/dev 说明](scripts/dev/README.md) —— 一次性脚本的运行方式与生产保护

## 仓库结构

```
bot/         飞书机器人（WS 长连接）
web/backend  Flask H5 后端      web/frontend  静态 H5 页面
lib/         bot 与 H5 共享代码（飞书客户端 / 多维表格 DAO / 存储 / 共享业务逻辑）
scripts/ops  守护/运维脚本      scripts/dev   一次性/排查脚本（勿用于生产，见其 README）
deploy/      systemd 服务模板与 nginx 配置
docs/        文档                tests/        pytest 单测
data/        运行时 JSON 与积分账本（gitignore；生产实际路径见部署文档）
.github/     GitHub Actions CI：语法/模板绑定检查 + ruff + pytest（触发条件见 workflow）
```

## 文档约定

本仓库文档只描述**稳定内容**——结构、路径、架构、原则与流程；不收录易变细节：

- 数值、端点/接口、文案、脚本清单、字段等易变信息，一律以**代码为唯一权威**（如快照刷新间隔见 `web/backend/app.py` 的 `_SNAPSHOT_INTERVALS`；脚本清单以 `scripts/` 目录为准）。
- 功能、文案、数值的变更**无需同步文档**；仅当结构或流程本身变化（目录布局、部署方式、跨进程一致性规则等）才更新文档。
