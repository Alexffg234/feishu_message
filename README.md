# 飞书群聊消息采集系统

基于 **飞书事件订阅** 的实时长驻服务：机器人被拉入目标群后，群里每一条消息都会通过 `im.message.receive_v1` 事件推送到你的服务，程序解析并落盘。

## 方案说明

| 维度 | 本方案（事件订阅） |
|------|-------------------|
| 实时性 | ✅ 实时推送，机器人所在群的新消息秒级到达 |
| 运行方式 | 常驻服务（FastAPI + Uvicorn） |
| 历史消息 | ❌ 只拿到机器人入群之后的消息（如需历史，另加历史拉取 API） |
| 依赖 | 公网 HTTPS 回调地址 |

## 项目结构

```
feishu_group_message/
├── main.py               # 主入口: FastAPI 服务,承载事件回调
├── requirements.txt      # 依赖
├── read_messages.py      # 查看/导出已采集消息
├── config/
│   ├── .env.example      # 配置模板(复制为 .env)
│   └── config.py         # 配置加载
├── app/
│   ├── handler.py        # 消息事件解析与处理
│   └── store.py          # 消息落盘(JSON Lines)
└── storage/
    └── messages.jsonl    # 采集到的消息(运行时生成)
```

## 快速开始

### 1. 创建飞书企业自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn) → 开发者后台 → 创建企业自建应用
2. 在「凭证与基础信息」复制 **App ID** 和 **App Secret**
3. 在「添加应用能力」开启 **机器人**
4. 在「权限管理」添加权限并**提交企业管理员审批**：
   - `im:message`（获取群组中所有消息，只读）
   - `im:message.group_at_msg`（接收群聊中@机器人消息事件）
   - `im:message.group_msg`（获取群组中所有消息）
   - `im:chat`（获取群组信息）
5. 在「事件与回调」→ 订阅方式选「将事件发送至开发者服务器」，添加事件 **接收消息 `im.message.receive_v1`**
6. 把「请求地址」填为你的回调地址（见下面第 4 步），复制 **Verification Token** 和（如开启加密）**Encrypt Key**

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

```bash
cp config/.env.example config/.env
# 编辑 config/.env, 填入 APP_ID / APP_SECRET / VERIFICATION_TOKEN / ENCRYPT_KEY
```

### 4. 启动服务（需要公网地址）

飞书需要把你的**公网 HTTPS 地址**配置为回调。本地开发用内网穿透：

```bash
# 方式A:内网穿透(推荐开发期)
ngrok http 8008
# 得到一个 https://xxxx.ngrok-free.app, 把 https://xxxx.ngrok-free.app/feishu/event
# 填到飞书「事件与回调」的请求地址

# 方式B:直接启动(生产环境已部署到有公网IP的服务器时)
python main.py
```

### 5. 把机器人加进目标群

在飞书里把应用机器人添加到你想要采集消息的群，之后群里的消息就会实时写入 `storage/messages.jsonl`。

### 6. 查看消息

```bash
python read_messages.py            # 打印最近100条
python read_messages.py --limit 20
python read_messages.py --json export.json   # 导出为JSON
```

## 鉴权提示

`/` 根路径会返回服务健康信息：

```
GET /       -> {"status":"running","messages_count":123, ...}
GET /health -> {"status":"ok"}
```

## 常见问题

- **收到事件但无消息入库**：确认机器人已被拉入该群，且应用权限被管理员审批通过。
- **回调地址校验失败**：飞书会先发一个 URL 校验请求（`challenge`），SDK 已自动处理；确认请求地址与 `event_path` 完全一致（`/feishu/event`）。
- **只想要特定群**：在 `.env` 填写 `ALLOWED_CHAT_IDS=oc_xxx,oc_yyy`。

## 安全

- `config/.env` 含敏感凭据，已加入 `.gitignore`，**不要提交到仓库**。
- 消息内容未经加密直接落盘，注意存储权限。
# feishu_message
