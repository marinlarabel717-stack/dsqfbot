# dsqfbot

`dsqfbot` 是一个按钮式 Telegram 管理机器人，用来管理多个 `session` 账号，批量导入群链接、智能间隔加群、同步群列表，并创建 Telegram 原生定时消息。

## 当前功能

- 按钮式首页面板，不依赖 slash 指令
- 通过机器人流程添加 `session` 账号
- 同步某个账号的群/频道列表
- 批量导入群链接，支持：
  - `均分分配`
  - `全部账号都加`
- 加群队列按间隔慢慢执行
- 群详情页显示：
  - 加入状态
  - 发言状态
  - 最近错误
- 在群详情页创建定时消息
- Premium 账号支持 `每天重复`
- 任务列表可以查看并停用任务

## 目录

```text
dsqfbot/
  dsqfbot/
    app.py
    config.py
    db.py
    telegram_mtproto.py
    utils.py
  storage/
    sessions/
  .env.example
  requirements.txt
  README.md
  main.py
```

## 安装

```bash
cd dsqfbot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 配置

把 `.env.example` 复制成 `.env`，至少填这些：

```env
BOT_TOKEN=你的机器人token
ADMIN_IDS=你的TG数字ID
API_ID=你的telegram_api_id
API_HASH=你的telegram_api_hash
DATABASE_PATH=storage/dsqfbot.sqlite3
SESSION_DIR=storage/sessions
DEFAULT_JOIN_INTERVAL_SECONDS=60
REPEAT_LOOKAHEAD_MINUTES=5
DEFAULT_TIMEZONE=Asia/Shanghai
```

说明：

- `BOT_TOKEN`：管理机器人的 token
- `ADMIN_IDS`：允许操作后台的 TG 用户 ID，多个用逗号分隔
- `API_ID / API_HASH`：Telethon 登录用户号需要
- `SESSION_DIR`：用户号 session 文件目录

## 启动

```bash
python main.py
```

## 使用流程

1. 给机器人发任意一条消息
2. 点 `账号管理`
3. 点 `添加账号`
4. 按提示发送：
   - 账号备注
   - 手机号
   - 验证码
   - 二步密码（如果有）
5. 点 `同步群组`
6. 点 `批量加群`
7. 选 session、分配模式、间隔，发群链接
8. 等群进入列表后，点 `新建定时消息`

## 已知边界

- 公开群和邀请链接可以自动处理
- 审批制邀请链接会显示 `awaiting_approval`
- 某些群发不了言时，会把错误翻译成：
  - `禁言`
  - `无发言权限`
  - `未加入群`
  - `账号掉线`
- 当前 `每天重复` 由后台持续补未来任务，依赖服务保持运行
