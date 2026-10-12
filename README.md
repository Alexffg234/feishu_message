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
feishu_message/
├── run.py                  # 启动入口
├── requirements.txt        # 依赖
├── config/
│   └── .env.example        # 配置模板(复制为 .env)
├── scripts/
│   ├── init_db.py          # 初始化数据库
│   └── query_messages.py   # 查看/导出/分组查询已采集消息
└── app/
    ├── main.py             # FastAPI 应用工厂
    ├── api/routes.py       # HTTP 路由(事件回调 + 查询接口)
    ├── core/               # 配置 / 数据库 / 日志
    ├── models/message.py   # 消息 ORM 模型
    ├── schemas/message.py  # Pydantic schema
    ├── dao/message_repo.py # 数据访问层
    └── services/           # 事件处理 / 业务逻辑(含分组)
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
python scripts/query_messages.py                 # 打印最近100条
python scripts/query_messages.py --limit 20      # 最近20条
python scripts/query_messages.py --chat oc_xxx   # 按群过滤
python scripts/query_messages.py --groups        # 按回复链分组输出
python scripts/query_messages.py --topics        # 按 LLM 主题聚合输出
python scripts/query_messages.py --reclassify    # 对未分类的组重新调 LLM 打标签
python scripts/query_messages.py --json export.json   # 导出为JSON
```

## 消息分组(回复链)

按「回复链」把消息归组:**A 发一条消息,B 回复 A(或 C 再回复 B),归为同一组**。

- 飞书回复消息的 `im.message.receive_v1` 事件自带 `parent_id`(被回复消息)与 `root_id`(回复链根)。
- 入库时冗余计算 `root_message_id` 作为分组键:独立消息 `root=自身`;回复消息沿父链归到同一根。
- 父消息未被采集到(如机器人入群前发的)时,以被回复消息为分组锚点。

HTTP 查询:

```
GET /messages/groups?chat_id=oc_xxx&limit=20
```

返回每组 `{root_message_id, chat_id, root, replies, message_count, senders}`。

## 主题分类(LLM)

在回复链分组之上再按「主题」聚合:把讨论同一类问题(如「投屏」「远场语音唤不醒」)的所有问答组聚到一个主题下,形成 **主题 → 回复链组 → 消息** 三层结构。

- 每个回复链组入库后,由**后台线程**调用 LLM(OpenAI 兼容接口)打一个 2~10 字的主题标签,不阻塞消息落库。
- 分类时把库里**已有主题列表**一并传给 LLM,同义话题沿用同一标签,保证聚合稳定。
- 在 `config/.env` 配置 `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` 启用;留空则禁用(消息正常入库,只是不归类)。
- 未分类的历史组可用 `python scripts/query_messages.py --reclassify` 补打标签。

HTTP 查询:

```
GET /messages/topics?chat_id=oc_xxx&limit=20
```

返回每个主题 `{topic, group_count, message_count, senders, groups:[...]}`。

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
