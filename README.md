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
│   ├── init_db.py                  # 初始化数据库(含对话表)
│   ├── create_user.py              # 命令行创建/改密/停用账号
│   ├── query_messages.py           # 查看/导出/分组查询已采集消息
│   ├── backfill_conversations.py   # 存量消息回填成对话关系(幂等)
│   └── query_conversations.py      # 查询对话/回复树
└── app/
    ├── main.py             # FastAPI 应用工厂(静态资源挂载 + 访问控制中间件)
    ├── api/routes.py       # HTTP 路由(事件回调 + 查询接口)
    ├── api/auth.py         # 登录注册接口 + 全站访问控制中间件
    ├── static/             # 前端资源(挂载到 /static,登录页在此)
    │   ├── login.html          # 登录/注册页结构(语义化 + ARIA)
    │   ├── login.css           # 设计令牌 + 布局 + 组件 + 响应式
    │   └── login.js            # 交互(校验/强度/切换/提交),无全局污染
    ├── core/               # 配置 / 数据库 / 日志
    ├── models/             # ORM 模型(message / conversation / user ...)
    ├── schemas/message.py  # Pydantic schema
    ├── dao/                # 数据访问层(message / conversation_repo)
    └── services/           # 事件处理 / 业务逻辑(含分组、主题、认证)
```

### 前端资源

页面为**独立静态文件**(非 Python 内联字符串),挂载在 `/static`,由路由返回:

| 文件 | 作用 |
|---|---|
| `static/components.css` | **公共层**:全站唯一设计令牌 + 顶部导航 + 通用组件(卡片/按钮/下拉/徽章/空态) |
| `static/nav.js` | **顶部导航**:动态渲染到 `#app-nav`,展示当前用户与退出入口 |
| `static/login.{html,css,js}` | 登录 / 注册页 |
| `static/summary.{html,css,js}` | 群聊总结页 |
| `static/habits.{html,css,js}` | 对话习惯分析页 |

约定与注意事项:

- **令牌唯一来源**:所有页面的颜色/圆角/动效都取自 `components.css` 的 `:root`,
  页面 CSS 只写专属布局,不得重复定义 `:root`(旧页面用的 `--bg/--card/--border`
  已作为兼容别名映射到新令牌)。
- **全站深色**:登录页与业务页统一深色主题;主色 `#185fa5` 在深色下提亮为 `#3b8ede`,
  辅色 `#0f6e56` 提亮为 `#3fc79a`。
- **顶部导航**:页面只需放 `<div id="app-nav"></div>` 并引入 `nav.js`,
  导航(含页面切换、当前用户、退出登录)由 JS 统一渲染,改一处全站生效。
- 认证中间件对 `/static/` 前缀放行,否则未登录时 CSS/JS 会被 401 拦截。
- 改样式/交互只需改 `app/static/` 下的文件,**不必重启服务**(强刷即可);
  资源带版本号 `?v=1.0.0`,改动后递增版本号可强制刷新缓存。

## 对话关系存储

消息除详情落 `messages` 表外,回复链作为**独立实体**存储,支持任意深度的回复树:

- `conversations`：一条回复链 = 一个对话(含根消息、参与人、时间范围、主题、消息数)
- `message_links`：每条"回复"边 child→parent(含 depth),完整保留多层级树(A→B→C→D)

新建库自动建表;存量库执行 `init_db.py` 补齐 `messages.conversation_id` 列,再执行
`backfill_conversations.py` 把已有回复关系回填成对话。

```bash
python scripts/init_db.py                 # 建表/加列
python scripts/backfill_conversations.py  # 回填存量对话关系
python scripts/query_conversations.py --limit 20       # 最近对话
python scripts/query_conversations.py --tree <id>      # 某对话的完整回复树
```

### 群聊总结可视化页面

内建一个轻量可视化页面,用于查看一次完整对话及其 LLM 总结文案(验证分类/聚合效果):

- `GET /summary`：可视化页面(对话下拉 + 总结卡片 + 回复树)
- `GET /api/conversations`：最近对话列表
- `GET /api/summary/{conversation_id}`：某对话详情 + LLM 总结 + 回复树

> 页面需配置 `LLM_API_KEY / LLM_MODEL` 才会出现总结文案;未配置时显示占位并正常展示回复树。

## 访问控制(登录注册)

默认开启全站登录校验:**未登录不能访问任何页面或接口**。

- 页面 `GET /summary`、`/habits`:未登录 302 跳转到 `/login?next=原路径`
- 接口 `GET /api/*`:未登录返回 401 JSON
- **飞书事件回调 `/feishu/event` 始终放行**(无 cookie,拦截会导致消息采集中断)
- `/login`、`/api/auth/*`、`/health`、`/ready` 放行

### 首次使用

启动后访问 `http://<host>:8008/login`,注册**首个账号自动成为管理员**。也可以先用命令行开号:

```bash
python scripts/create_user.py admin 你的密码          # 创建(首个账号=admin)
python scripts/create_user.py admin --reset-pwd 新密码 # 改密
python scripts/create_user.py someone --disable        # 停用(保留数据,禁止登录)
python scripts/create_user.py --list                   # 查看全部账号
```

登录后页面右上角有「退出登录」入口,或直接访问 `GET /logout`。

### 相关配置(config/.env)

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUTH_ENABLED` | `true` | 关闭即全站免登录(仅本地调试,生产勿关) |
| `ALLOW_PUBLIC_REGISTER` | `true` | 关 false 后只能用 `create_user.py` 开号;**库里无账号时注册恒开放**,否则无法创建首个管理员 |
| `SESSION_TTL_HOURS` | `12` | 登录态有效期 |
| `SESSION_SECRET` | 空 | 会话签名密钥;留空自动生成并持久化到 `data/.session_secret`。**多实例部署必须显式配成同一个值** |
| `COOKIE_SECURE` | `false` | 站点为 https 时必须置 `true` |

### 密码复杂度要求

注册与改密(`create_user.py --reset-pwd`)都必须满足,**已存在的旧密码不受影响,仍可正常登录**:

1. 长度 8~128 位
2. 同时包含字母和数字
3. 不能是常见弱密码(内置黑名单:12345678 / password / qwerty12 / admin123 等)
4. 同一字符不连续重复 4 次以上(如 `1111abcd`)
5. 不含 5 位以上连续字符(如 `12345`、`abcde`)

页面会实时显示强度条与规则清单(逐条打勾),全部满足才能提交;**后端会二次校验**,绕过前端直接调接口同样会被拒,并返回具体原因。
规则定义在 `app/services/auth_service.py` 的 `PASSWORD_RULES` / `password_issues()`,前端清单文案由接口下发,改规则只需改后端。

### 实现说明

- 零新增依赖:密码用标准库 `hashlib.pbkdf2_hmac`(SHA256 + 每账号随机盐,20 万轮),会话用 HMAC-SHA256 签名 cookie
- 会话无状态(不建 sessions 表),登出即清 cookie;密钥落 `data/.session_secret`,重启后不会强制下线
- 登录失败提示不区分「用户名不存在」与「密码错误」;同一账号连续失败 5 次冷却 5 分钟(进程内计数,更强的防护请放在 nginx/网关层)
- 密码只存摘要,库里无明文

## 消息资源(图片/视频/文件 → 七牛对象存储)

飞书资源类消息的 `content` 里只有资源 key(图片 `image_key`、视频 `file_key`,如 `{"image_key":"img_v3_...","file_key":"file_v3_...","file_name":"x.mp4","duration":71033}`),这些 key 是**临时凭证会过期**,本身不是可直接展示的 URL。

本系统在**收到资源类消息时**自动完成「下载 → 上传七牛 → 生成可展示 URL」并落库:

| 消息类型 | 下载内容 | 落库字段 | 展示 |
|---|---|---|---|
| image | 图片(image_key, `type=image`) | `image_url` | `<img>` |
| media(视频) | 视频本体(file_key, `type=file`) + 封面(image_key) | `file_url` + `image_url`(封面) | `<video>`(带封面) |

```bash
# config/.env 配置七牛(缺任一则资源消息保留 key,优雅降级不报错)
QINIU_ACCESS_KEY=你的AK
QINIU_SECRET_KEY=你的SK
QINIU_BUCKET=你的空间名
QINIU_DOMAIN=https://cdn.你的域名    # 七牛 CDN 访问域名,不带尾斜杠
IMAGE_DOWNLOAD_ENABLED=true
```

关键点:
- **收到即落盘**:资源 key 会过期,不能等展示时再下载,所以入库时同步下载上传。
- **下载接口**:用[获取消息中的资源文件] `GET /im/v1/messages/{message_id}/resources/{file_key}?type=image|file`。
  - 图片/封面用 `type=image`;视频/音频/文件用 `type=file`。
  - ⚠️ 不要用 `/im/v1/images/{image_key}`(那是给机器人自己上传的图用,下载用户发的资源会返回 **234001 Invalid request param**)。
- **权限**:需要飞书应用开通 `im:resource`(读取消息中的文件资源)权限,机器人须在群内。
- 上传 key 规则:`qiniu/images/{key}`(图片)、`qiniu/files/{key}`(视频),天然去重、可对回原资源。
- 历史消息可补:服务器上跑 `python ./.venv/bin/python scripts/backfill_images.py [--limit 500]` 回填(幂等可重复)。
- 列表/总结接口的树节点都带 `image_url`/`file_url`(已落盘)与 `image_key`/`file_key`(原始凭证)字段。

> ⚠️ 服务器上运行 python 脚本务必用项目 venv:`./.venv/bin/python scripts/xxx.py`(系统 `python` 是 conda base,没装项目依赖会报 `ModuleNotFoundError: sqlalchemy`)。

## 服务器热更新

代码合入 `main` 后,在服务器上一条命令完成「拉代码 → 更新依赖 → 建表 → 回填对话关系 → 重启 → 健康检查」:

```bash
# 在服务器 /opt/feishu_group_message 下执行
sudo bash deploy/update.sh
```

要点:
- `git fetch`(纯网络)与 `git merge --ff-only`(纯本地)分开,网络闪断不影响合并
- 更新前若工作区有未提交改动会**中止并提示**,绝不覆盖本地修改
- `init_db.py` / `backfill_conversations.py` 均幂等,可重复执行
- 部署后访问 `http://<服务器>:8008/summary` 验证总结页

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

### 轮次合并(同一人连发多条)

同一个人短间隔连发的多条独立消息会合并进**同一个消息块**，判定条件(全部满足)：

1. 发送者相同(`sender_id` 一致)
2. 与上一条同发送者消息的间隔 ≤ `TURN_MERGE_SECONDS`(默认 30 秒，0=禁用)
3. 中间若插入其他人的消息，该消息须属于同一块(如同组回复)，否则视为打断、不合并

合并后的消息块与回复链、主题维度正交叠加：A 连发 3 条、B 回复其中一条，4 条同属一个块/对话。

### 同主题归组(无回复的碎片反馈归回原问题块)

「直接发言无回复」的追问/反馈(有人甚至隔半天才回，时间窗不可靠)无法靠回复链归组，
等该组的 **LLM 主题标签** 打完后，按标签归并：

- 同一群内、topic 标签**完全相同**的多个回复链组 → 归并为一个对话，**最早组保留为根**(原问题块)。
- 归并迁移 `messages.root_message_id/conversation_id`、`message_links` 回复边，删除多余对话行并重算统计；幂等可重复执行。
- 提示词机制(保证碎片组贴出一致标签):
  - **前文上下文**:分类时附该组之前同群最近 6 条消息(带各自话题标签),当前组是前面讨论的延续时沿用其标签;
  - **已有主题摘要**:附各主题最近消息摘要,同一问题沿用已有标签,避免碎片组被贴成更细的同义标签;
  - **纯附件组**(整组只有图片/视频/语音、无文本)不打标签,不产「图片」这类泛标签,保持独立成组;
  - **语气词守卫**:「好的/收到/嗯」等纯语气词视为空标签重试,避免垃圾标签污染归并;
  - **近义标签归并**(`--consolidate`):LLM 输入全部标签 + 各标签案例开场样本,判定哪些标签属同一故障案例(开场症状+诊断+处理+验证),统一到保留标签后再触发同标签归并。
- 开关：`TOPIC_MERGE_ENABLED=False` 时只打标签不归并。
- 存量数据回填：

```bash
python scripts/rethread_backfill.py --dry-run                 # 预览将归并的组对
python scripts/rethread_backfill.py --reclassify              # 先给未分类组补标签,再归并
python scripts/rethread_backfill.py --relabel --consolidate   # 清空标签按时间序重打(前文带标签)+近义标签归并,再归并
python scripts/rethread_backfill.py --chat oc_xxx             # 仅处理指定群
```

### 会话缝合(同一问题处理过程被拆散时合并)

同标签归并依赖 LLM 标签;当用户「只 @ 不回复」、各组无标签或标签是垃圾词
(如「好的」)时,同一问题处理过程(提问→排查→多轮对话→给出解决方案)会拆成多个会话。
会话缝合不依赖标签:把相邻会话的代表消息(首条/末条/最长)直接送 LLM 按生命周期判定,
重叠窗口 + 并查集传递闭包,把同过程会话合并到最早会话(复用 `merge_groups`,幂等)。

```bash
# 本地验证(推荐):先只读导出到本地 sqlite,在副本上改,真实库不动
python scripts/export_chat.py --chat oc_xxx --out data/local.sqlite
python scripts/stitch_conversations.py --chat oc_xxx --db data/local.sqlite           # 预览拟合并对
python scripts/stitch_conversations.py --chat oc_xxx --db data/local.sqlite --apply   # 在本地副本执行

# 直接在库上执行(默认预览,--apply 才写库);循环「预览→apply」直到无拟合并对
python scripts/stitch_conversations.py --chat oc_xxx --apply
```

## 主题分类(LLM)

在回复链分组之上再按「主题」聚合:把讨论同一类问题(如「投屏」「远场语音唤不醒」)的所有问答组聚到一个主题下,形成 **主题 → 回复链组 → 消息** 三层结构。

- 每个回复链组入库后,由**后台线程**调用 LLM(OpenAI 兼容接口)打一个 2~10 字的主题标签,不阻塞消息落库。
- 分类提示词 = 前文最近 6 条消息(带话题标注) + 已有主题列表(附最近消息摘要) + 当前组内容,同一问题沿用同一标签,保证聚合稳定。
- 打完标签即触发上面的**同主题归组**;`conversations.topic` 随分类同步写入(总结页/列表展示)。
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
- **忘记密码**：`python scripts/create_user.py 用户名 --reset-pwd 新密码`
- **页面显示「自助注册已关闭」**：库里已有账号且 `ALLOW_PUBLIC_REGISTER=false`,用 `create_user.py` 开号。
- **登录成功仍跳回登录页**：站点是 https 但 `COOKIE_SECURE=false`,浏览器不会回传 cookie。
- **多实例部署反复掉线**：各实例 `SESSION_SECRET` 不一致,配成同一个值。

## 安全

- `config/.env` 含敏感凭据，已加入 `.gitignore`，**不要提交到仓库**。
- 消息内容未经加密直接落盘，注意存储权限。
- 全站默认要求登录（见「访问控制」）；`data/.session_secret` 是会话签名密钥，**泄露即可伪造任意用户的登录态**，须与 `.env` 同等对待（`data/` 已忽略）。
# feishu_message
