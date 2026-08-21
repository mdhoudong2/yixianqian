# 一线牵

面向教会青年的婚恋交友平台 —— 飞书机器人 + H5 双端，数据存于飞书多维表格（Bitable）。

> 生产环境：<https://app.nantou.love> ｜ 机器人：飞书搜索「一线牵」

## 功能特性

**飞书机器人（bot/）**
- 注册表单引导、人工审核通过后自动开通 H5
- 匿名喜欢 / 相互喜欢揭晓（自动互发对方飞书名片）
- 爱心机制：初始 3 颗，邀请好友注册 +1（上限 30）
- 活动报名与通知：实名通知喜欢 TA 的人、匿名通知 TA 喜欢的人
- 分组匹配：按活动设置男女比例收集志愿，生成分组结果并推送
- 主菜单卡片（卡片1）交互，消息中心通知汇总

**H5 网页（web/）**
- 左右滑动卡片浏览异性资料、点心表达心意
- 消息中心：谁喜欢了我 / 相互喜欢
- 活动列表与报名、分组志愿提交与结果查看
- 个人资料查看与修改
- 引流页 `public.html`（匿名浏览 + 注册转化埋点）

## 架构

```
飞书用户 ──消息/卡片──▶ 飞书开放平台 ──WS 长连接──▶ bot/yixianqian_bot_ws.py
用户浏览器 ──HTTP──▶ Nginx ──▶ Flask (web/backend)     │
                                │  读/写           写/读│
                                ▼                     ▼
                           多维表格 Bitable ◀──────────┘
                               ▲
                    运行时共享 JSON（通知等，SHARED_DATA_DIR）
```

- **数据层**：全部业务数据存飞书多维表格，无自建数据库
- **bot ↔ H5 通信**：通过共享运行时 JSON（`SHARED_DATA_DIR`，机器人写、H5 读）
- **H5 性能**：后端本地快照缓存 + 15s 定时刷新（gunicorn `post_fork` 预热），避免每次请求直连飞书 API
- **AI 能力**：匹配推荐评分、图像识别（ModelScope）、联网检索（Tavily）等位于 `bot/tools/`

## 技术栈

| 端 | 技术 |
|---|---|
| 机器人 | Python 3.12 · lark-oapi（WS 长连接） |
| H5 后端 | Flask · gunicorn · Pillow · itsdangerous |
| H5 前端 | Vue 3（CDN）· Vant 移动端组件 · Sortable |
| 数据 | 飞书多维表格 Bitable |
| AI | DeepSeek · ModelScope Vision · Tavily |

## 快速开始（本地开发）

```bash
git clone https://github.com/mdhoudong2/yixianqian && cd yixianqian

# 1. 配置密钥
cp bot/local_config.example.py bot/local_config.py
cp web/backend/local_config.example.py web/backend/local_config.py

# 2. 机器人
python3 -m venv bot/venv && bot/venv/bin/pip install -r bot/requirements.txt
cd bot && ./venv/bin/python yixianqian_bot_ws.py

# 3. H5 后端（端口 8091）
python3 -m venv web/backend/venv && web/backend/venv/bin/pip install -r web/backend/requirements.txt
cd web/backend && ./venv/bin/gunicorn -c gunicorn.conf.py app:app
```

## 文档

- [部署与协作](docs/DEPLOY.md) —— 生产部署、发布流程、服务器操作
- [文案对照表](docs/文案对照表.md) —— 机器人全部用户消息文案
- [scripts/dev 说明](scripts/dev/README.md) —— 一次性脚本用法

## 仓库结构

```
bot/         飞书机器人（WS 长连接 + AI 工具）
web/backend  Flask H5 后端      web/frontend  静态 H5 页面
scripts/ops  守护/运维脚本      scripts/dev   一次性/排查脚本（勿用于生产）
deploy/      systemd 服务模板   docs/         文档
```
