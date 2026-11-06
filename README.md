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
│   ├── query_conversations.py      # 查询对话/回复树
│   ├── rethread_backfill.py        # 存量同标签归并/重打标签
│   ├── stitch_conversations.py     # 手动会话缝合(LLM 生命周期判定)
│   ├── merge_conversations.py      # 手动把多个对话归并为一个案例
│   ├── move_messages.py            # 把某对话时间窗内消息挪入另一对话
│   └── backfill_block_start.py     # 存量对话回填新问题块起点(横杠)标记
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
| `static/review.{html,css,js}` | 标签复核页(`/review`):低置信队列 → 人工定标 → gold set |
| `static/metrics.{html,css,js}` | 标签指标页(`/metrics`):把 `/api/topic_metrics` 翻译成「现在该做什么」 |

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

- `GET /summary`：可视化页面(对话下拉 + 总结卡片 + 完整对话按时间平铺 + 横杠分隔不同问题)
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
| `ALLOW_PUBLIC_REGISTER` | `false` | 默认关闭公开注册(安全默认),需建/管账号用 `create_user.py`;**库里无账号时注册恒开放**,否则无法创建首个管理员 |
| `SESSION_TTL_HOURS` | `12` | 登录态**绝对**有效期(小时)，从登录时刻算起，滑动续期也不会超过它 |
| `SESSION_IDLE_TIMEOUT_MINUTES` | `10` | **空闲超时**(分钟)：连续这么久没有任何请求即自动登出；置 `0` 表示只按绝对有效期失效 |
| `SESSION_SECRET` | 空 | 会话签名密钥;留空自动生成并持久化到 `data/.session_secret`。**多实例部署必须显式配成同一个值** |
| `COOKIE_SECURE` | `false` | 会话 cookie 是否仅 HTTPS 传输。**看浏览器访问的协议，不是看应用监听的协议**：站点对外是 http 就保持 `false`；对外是 https 必须置 `true`（即使用 nginx 做了 SSL 终结、FastAPI 本身只监听 127.0.0.1 的 http） |
| `ALERT_WEBHOOK` | 空 | 飞书机器人群机器人 webhook。服务异常/数据不新鲜时由 `scripts/health_probe.py` 推送告警；留空则不推送 |
| `ALERT_NO_MESSAGE_MINUTES` | `30` | 超过该分钟数没有新消息入库即视为「数据不新鲜」，触发告警；`0` 不检查 |
| `ALERT_DEAD_LETTER_THRESHOLD` | `3` | 死信事件行数超过该值视为异常（事件持续处理失败） |

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

> **多 worker 协调(生产 `gunicorn --workers 2`)**:主题分类(LLM)、同主题归并、
> 登录限流均已 DB 化互斥,跨 worker 正确,可放心多 worker 提高吞吐。
> 事件队列为每 worker 独立 FIFO,跨 worker 不保证同一 chat 严格有序;解析已容错
> (message_id 唯一索引幂等 + 回复消息用事件 root_id 兜底锚组),仅「同人连发轮次合并」
> 的归组质量在乱序时可能略有偏差(不丢数据)。如需严格按 chat 保序,
> 改 `--workers 1` 或 `--worker-class gthread` 即可。

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
重叠窗口推进(不做跨窗传递闭包,防链式放大过度合并),把同过程会话合并到最早会话
(复用 `merge_groups`,幂等)。护栏:双方 topic 冲突不合并(已有确定标签是强反证);
时间间隙不作护栏,是否同一案例完全交由 LLM 判定。

- **后台自动调度**(默认开启):服务启动后每 `CONV_STITCH_INTERVAL_MINUTES`(默认 30)分钟
  自动跑一轮,只处理水位线(会话数/最大消息 id)有变化的群;
  `CONV_STITCH_ENABLED=false` 或间隔设 0 禁用。缝合 = 同一案例续聊,合并后**不画横杠**。
- **手动脚本**:

```bash
# 本地验证(推荐):先只读导出到本地 sqlite,在副本上改,真实库不动
python scripts/export_chat.py --chat oc_xxx --out data/local.sqlite
python scripts/stitch_conversations.py --chat oc_xxx --db data/local.sqlite           # 预览拟合并对
python scripts/stitch_conversations.py --chat oc_xxx --db data/local.sqlite --apply   # 在本地副本执行

# 直接在库上执行(默认预览,--apply 才写库);循环「预览→apply」直到无拟合并对
python scripts/stitch_conversations.py --chat oc_xxx --apply
```

### 横杠分隔(block_start)与消息挪组

- `/summary` 完整对话按时间顺序平铺;带已有标签的新消息并入既有对话(新问题块)时
  标记 `messages.block_start=1`,前端在该消息前画横杠;同一案例的续聊(缝合)不打标记不断句。
- 人工修正工具:

```bash
python scripts/merge_conversations.py --ids 317,318,319 --topic 投屏卡顿 --no-block-start  # 多对话归并为一个案例
python scripts/move_messages.py --from 143 --to 317 --start "2026-10-26 16:50" --end "2026-10-26 17:20" --topic 投屏卡顿
```

### 问题单元切分(一组 = 一个问题)

同标签归并、会话缝合都在**合并**;但当一组里本来就混着几个不同问题时,无论 LLM
怎么选,单标签必然覆盖不全(审计:85 个回复链组里 39 组跨 2 小时以上,典型的一组
把 4 天内 4 件不相关的事并在一起,摘要还丢了关键线索)。所以打标签之前先按
**问题单元**把组切开:**一个对话一个标签**。

切点判据(纯规则、零 LLM):

| 信号 | 条件 | 说明 |
|---|---|---|
| 换设备 | `messages.case_key` 两侧都非空且不同 | 最强信号,必切 |
| 显式块起点 | `messages.block_start=1` | 归并打标时标记的新问题块 |
| 时间断档 | 相邻消息间隔 > `--gap-hours`(默认 2) | 上一轮求助已结束 |

三条取并集;切出来的片若只有客套句(「好的谢谢」)则并回上一片,图片/视频片不当噪声。

```bash
# 前置:先回填设备锚点(否则「换设备」这条判据全场失效)
python scripts/backfill_case_key.py --dry-run

python scripts/split_cases.py --stats                 # 各阈值的切分规模对比(不写库)
python scripts/split_cases.py --gap-hours 2           # 预览(默认 dry-run)
python scripts/split_cases.py --gap-hours 2 --apply   # 落库
python scripts/split_cases.py --gap-hours 2 --apply --relabel   # 落库并立即给新单元归类(需 LLM)
```

落库后**新单元**的 `topic`/`topic_conf` 清空等重新分类(旧标签是从混合组继承来的),
`stage` 按新组内文本重算;承接组首那个问题的 `keep` 组保留原标签。切分**不继承**
`case_id`:问题单元(conversation)是物理边界、一个单元一个标签,案例组(`case_id`)
是软关联、由缝合/`backfill_case_id` 重新判定——否则切完又被 `case_id` 粘回去。

跨切点的回复边留在父消息所在对话,子消息在两侧都按根渲染(`list_tree` 对「父不在本
对话」的消息按根处理),不丢消息。

#### 入库侧护栏:别让畸形对话继续长出来

`split_cases.py` 是**事后修补**;真正的入口在入库:`_resolve_root` 原来对回复消息无条件
`return parent.root_message_id`,于是「点着三天前的消息回复、问的却是全新问题」会把新问题
直接挂进旧对话——线上那些横跨数天的巨型对话就是这么长出来的。护栏规则:

> 整组**最后一条消息**距当前消息超过 `CASE_SPLIT_GAP_SECONDS`(默认 7200 秒)→ 当前消息
> 自成一组,不再并入旧组。

判据用整组最后一条而非被回复那条:组里刚有人说过话说明这轮处理还没结束,此时回复旧消息
属于延续;整组静默超阈值,再回复旧消息基本就是新问题。时间字段缺失/不可解析一律按旧行为
并入(脏数据不该成为把正常追问劈成两个对话的理由)。

```bash
# config/.env
CASE_SPLIT_GAP_SECONDS=7200   # 0 = 关闭护栏;建议与 split_cases.py --gap-hours 保持一致
```

护栏只做**物理新起一组**,不判断「是否仍属同一案例」:那是 `conversations.case_id` 的软关联
职责,由缝合/`backfill_case_id` 判定,可解绑。跨组边的 `message_links.depth` 按 1 记(父不在
本组时抄父树深度会产生「根消息深度 4」这种无意义值)。

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

### 候选召回(41 选 1 → 8 选 1)

闭集标签到 40+ 个之后,让 LLM 一次在全集里选,容易在近义标签间摇摆
(「画面模糊」/「画面模糊类问题」/「画质不清晰」),提示词也长。所以先纯字面**召回**
top_k 个候选,再让 LLM 在候选里做选择题:

| 打分路 | 说明 |
|---|---|
| 精确命中 | 文本直接包含标签名 → 满分(最强信号) |
| 覆盖度 | 标签 2-gram 出现在文本里的比例(长文本下 Dice 会被稀释到接近 0,不能用) |
| 样例相似 | 该标签**历史消息**与当前文本的 Dice × 0.9——标签名不含字面时唯一起作用的一路 |

三条取最大,且召回**只窄化、不放宽**:

- 候选永远是标准集子集,下游闭集校验照旧生效;
- 标签总数 ≤ `top_k`、`top_k=0`、或全部分数为 0(没有任何字面信号)时**不做窄化**——
  瞎猜式的窄化比不窄化更糟;
- 窄化后 LLM 判 `UNKNOWN` → **用全集重问一次**(只在失败路径多一次调用)。

```bash
TOPIC_RECALL_TOP_K=8    # 0 = 关闭(提示词给全集);标签超过 60 个可调到 10~12
```

实测 41 个标准标签、`top_k=8`:提示词 511 → 227 字符(压缩 56%),判别结果不变。
漏检率看 `/api/topic_metrics` 的 `recall.fallback_rate`(窄化后仍判 UNKNOWN 的比例),
持续偏高说明 `top_k` 太小或样例不足。

已知局限:候选可能集中在同一大类(「画面糊了」会把 7 个含「画面」的标签全部召回)。
这不是 bug——正确答案就在那一族里,候选给了最好的对照;真答案在别的大类时模型会判
`UNKNOWN`,由全集重问兜底。

### 标签治理(闭集上线后最该盯的几件事)

闭集判别的取舍是「宁可空着,也不写脏标签」。所以真正的风险从「标签写错」变成
**「未分类静默堆积」**:LLM 选不出标准标签就输出 `UNKNOWN`,那组落成未分类,
而 `list_topics` 只查 `topic != ''`,页面上根本看不见这批数据。三个配套入口:

```bash
# 1. 未分类清单:哪些组没打上标签、它们在说什么(+ 高频词,看闭集缺哪类现象)
python scripts/unclassified.py --words --limit 100

# 2. 别名表冷启动:非标准标签 -> 最相近标准标签的建议(默认只打印,Dice 打分)
python scripts/suggest_aliases.py
python scripts/suggest_aliases.py --apply --min-score 0.6    # 达标的写入 topic_aliases

# 3. 组内空标签 + conversations.topic 回填(运行时已自动做,这里补历史存量)
python scripts/backfill_group_topic.py --dry-run
python scripts/backfill_group_topic.py
```

- **相似度兜底默认开启**(`TOPIC_ALIAS_SIM_THRESHOLD=0.65`):闭集要求 LLM 输出
  标准标签原文,但实测常有 near-miss(少写/多写「类问题」后缀,
  如「画面模糊」vs「画面模糊类问题」dice=0.67)。不给兜底,这类输出会被闭集
  直接判死成未分类——那是**可避免的**未分类。兜底之后仍有闭集校验,即便映射到
  不存在的标签也只落回未分类,不会写脏标签。设 0 可回到「只认精确匹配」。
- **已打标组的新消息会零成本回填**:组已有标签时不再调 LLM,只把新到消息的
  `topic` 补成组内标签并同步 `conversations.topic`(旧行为是直接跳过,
  导致同组内标签不一致 14 组/40 条)。
- **别名表只影响之后的分类收敛**,不会改写历史 `messages.topic`。要改写存量需另跑
  `label_merge`(近义归并,会物理合并同标签组),建议放在
  `scripts/split_cases.py` 切分**之后**,避免把不同问题重新并到一起。

标签质量的度量在 `GET /api/topic_metrics`(只读):

| 字段 | 含义 | 目标 |
|---|---|---|
| `unclassified.rate` | 未分类组占比 | 盯住不上升;上升说明闭集缺现象 |
| `standard_coverage.rate` / `unused_standards` | 标准标签被用到的比例 / 从没被用到的标准 | `unused_standards` 长期偏多 = 标准集有冗余项,或闭集缺别名映射 |
| `standard_coverage.labels` | **每个**标准标签被多少组用到(含 0) | 0 = 挂着没人用;页面靠它标出冗余项 |
| `standard_coverage.off_standard` | 库内用过、但**不在标准集**的标签 | 越少越好;这是别名表没收敛住的漏洞 |
| `confidence` | 已归类组的置信度分档(高≥0.8 / 中 / 低 / 无) | 中低档即人工复核队列 |
| `low_frequency_ratio` | 只用过 1 次的标签占比 | 衡量同义分裂是否被收敛 |
| `review.pending_groups` | 待人工复核组数(未分类 + 低置信) | 不降 = 闭集缺类或提示词不到位 |
| `review.gold_groups` | 人工确认过的组数(gold set) | **为 0 时上面所有准确率都只是模型自说自话** |

`labels` 和 `off_standard` 是一对:前者是「挂着没人用」的标签(标准集冗余),
后者是「用了却不在册」的标签(别名漏网)。两边都收敛到 0 附近,才算标签体系健康。

#### 指标页面(`/metrics`)

这套指标原来只能 curl,等于没人看。页面做的事不是「把数字画出来」,而是**把数字
翻译成动作**:每个指标过一遍阈值,汇成顶部诊断横幅,卡片只是给横幅提供证据。

- **诊断横幅**(最上面):`gold set = 0` 时红色警示「所有准确率不作数」;未分类率
  ≥15% 起警、≥30% 转红,并直接给出该跑哪个脚本;非标准标签漏网 >0 提示
  `suggest_aliases.py`;召回漏检率 ≥20% 提示调 `TOPIC_RECALL_TOP_K`。
- **6 个 KPI**:未分类率 / 标准标签覆盖率 / 非标准标签漏网 / 待复核组 / gold set 规模 / 库内标签总数。
- **标准标签用量**:按大类分组列出全部标签,括号里是组数;`未用过` 的 chip 用虚线弱化。
  瘦身前后看这一块最直观 —— 用量为 0 的标签应该越来越少。
- **非标准标签漏网**:红色 chip 列用量最多的 50 个(`off_standard_count` 仍是全量)。
- **归并与碎片 / 进程内指标**:后者的三项都是**进程内累计、重启归零**,只在
  卡片里明确写了这一点,否则每次部署后都会有人以为指标崩了。
- 接口 500 时页面不白屏,而是提示「多半是库缺 `topic_conf` / `stage` 列(代码没部署
  或没重启)」——这是最常踩的那个坑。

页面只读,不需要 admin;支持 30 秒自动刷新(切到后台暂停,切回来补一次)。

### 人工复核闭环(gold set)

闭集、召回、相似度兜底都只能把「模型判得对的」判对。剩下判不对的必须有人看一遍,
而且看一遍的价值要能沉淀,否则同一类错会一直错下去。沉淀路径:

```
人工把一组的标签改对
  -> 回写 messages/conversations(置 topic_source='human',置信度 1.0)
  -> 原标签是系统自造的非标准标签时,写进 topic_aliases(以后自动收敛)
  -> 该组进入 gold set -> 用它评准确率 -> 再调提示词/召回
```

```bash
python scripts/topic_review.py                          # 队列(未分类优先),带样本文本
python scripts/topic_review.py --export review.csv      # 导出:填好 label 列再倒回
python scripts/topic_review.py --csv review.csv         # 预览(不写库)
python scripts/topic_review.py --csv review.csv --apply # 执行
python scripts/topic_review.py --chat oc_xxx --group <root> --label 屏幕闪烁 --apply --propagate
python scripts/topic_review.py --gold                   # gold set 规模/标签分布
python scripts/topic_review.py --gold --eval            # 重跑判别算 top-1 准确率(需 LLM)
```

入队口径:整组无标签(未分类)**或**组内最大 `topic_conf < 0.5`(低置信);
`topic_source='human'` 的组不再进队列,否则人工审过的会被反复塞回。

两条硬护栏(踩过就知道多重要):

- **原标签是标准标签时绝不写别名**。把「屏幕闪烁」映射成「画面模糊类问题」会让
  以后所有「屏幕闪烁」都被改写——一次手滑毒化整个标签体系。同一护栏也加在了
  `POST /api/topic_aliases` 上,那是这个 bug 的第二个入口。
- **标签必须能收敛到标准集才落库**(除非 `--allow-new`)。人工也会手滑写错字,
  不能因为一次手滑就在库里造一个新标签;被拒时库原样不动。

`--propagate` 把库里其它同错标签的组一并回刷(默认关)。回刷只改 `topic`,
不动 `topic_source`/`topic_conf`——那些组既不是模型判的也不是人逐条看的,
不该算进 gold set。

评估准确率必须走与线上**同一条**判别路径(`predict_group` 复用
`_decide_label`),否则量出来的不是线上的准确率。

#### 复核页面(`/review`)

队列和 gold set 原来只有 curl 能看,而这套闭环的价值全在「有人真的去定标」——
没有页面就等于没人用。页面做的事和脚本完全一致,只是把逐条定标变成点一下:

- **顶部统计**:待复核组数 / 本页未分类 / 本页低置信 / gold 组数 / 人工覆盖率。
  「待复核」不降说明闭集缺类或提示词不到位;`gold_groups=0` 时所有准确率都不作数。
- **队列卡片**:未分类优先(问题更大),再按组内最新时间倒序;每组带样本文本、
  现值与置信度。输入框带标准标签提示(按大类分组),**回车即提交**。
- **提交反馈**:成功会说明「旧值 → 新值、影响几条、学了哪个别名、连带回刷几条」,
  卡片随即移出队列;失败(标签收敛不到标准集)会原样保留卡片并显示拒绝原因。
- **两个可选项**:`连带回刷同错标签` = 脚本的 `--propagate`;`允许闭集外` = `--allow-new`
  (慎用,建议只给真正缺失的现象用)。
- **导出 CSV**:字段与 `scripts/topic_review.py --export` 逐列一致,**页面上已填但没提交
  的值也会带上**,所以可以「先在页面批量填一遍 → 导出 → 交给脚本 `--csv --apply`」。
- **权限**:定标接口要求 `admin`。普通成员进页面看到的是只读态(横幅 + 禁用输入),
  不会填完才吃 403。

> 页面只负责「定标」这一个动作。批量/回刷/评估准确率仍在脚本里:
> `--gold --eval` 需要 LLM,不适合放进请求周期。

### 标准体系瘦身(41 → 33)

标签治理治的是「存量标签脏不脏」,瘦身治的是**体系本身在制造噪声**。生产库审计出三个结构病:

| 病 | 现象(生产库实测) | 后果 |
|---|---|---|
| **层级是反的** | 18 个「大组」里有 9 个其实是子组(「黑屏类问题」「画面模糊类问题」),真正的 `topics` 在子组身上;父组 `画质类问题` 的 `topics` 永远是空 | 白占一层,LLM 拿到的大类清单里混着具体现象,候选召回的层级也跟着错 |
| **动作词混进 topic** | 「换机推送」(108 条)、「已推」(25 条)、「上门维修」(8 条)、「画质推送」——这些是 `stage`(处理到哪一步),不是「遇到什么问题」 | 既污染主题统计,又让这类组永远无法正确归类:它们本来就没有现象标签 |
| **同义不收敛** | 「画质模糊 / 画质模糊推送升级」「酷喵黑屏 / 切换4K黑屏 / 开机黑屏有声音 / 有声无图」各算一个标签 | 库内实际用过 62 个标签,而标准集只有 41 个 —— 分裂就是这么来的 |

瘦身后的体系:11 个大类 / 33 个标准标签,大类只作语境,现象一律是统一粒度的闭集。
画质族 6 合 1(模糊/花屏/闪屏/黑屏/黑点/亮度)、语音族 3 合 1(唤醒/识别/网络)、
动作类标签整体摘出。

```bash
python scripts/slim_topics.py                          # 预览:现状 -> 方案,不写库
python scripts/slim_topics.py --apply                  # 写库(先自动落快照)
python scripts/slim_topics.py --restore data/topic_slim_backup-20261106-101500.json
python scripts/slim_topics.py --apply --force          # preflight 阻断时强行写(不建议)
```

四条不能省的设计:

- **合并是 N→1,不可逆,所以必须逐条快照**。`--apply` 前把每条 `messages.topic` /
  `conversations.topic` 的**原值**写进 `data/topic_slim_backup-<ts>.json`,
  `--restore` 按 id 逐条还原,而不是靠映射反推——映射反推只能还原出「一组里最像的那个」,
  分不开原本是三个标签的组。
- **preflight 会拦人**。摘掉「已推」「上门维修」等于丢掉唯一的进度信号,所以缺
  `messages.stage` 列时 **`--apply` 直接阻断**(缺 `topic_conf`/`topic_source` 只警告)。
  这是刻意的:生产库自闭集上线后没重启过就会命中这条,必须先部署重启(迁移在
  `init_db()` 里跑)再 `scripts/backfill_stage.py`,否则推完就没法追进度了。
- **旧名沉淀进 `topic_aliases`(`auto=0`)**,保证 LLM 以后再输出「酷喵黑屏」会自动
  收敛到「黑屏」,标签不会「长回来」。`auto=0` 表示这是人工定下的映射,不被自动建议覆盖。
- **清空的标签不是删掉,是回未分类队列**。12 个标签(含 `换机推送` 108 条)会落到
  未分类,由 `/review` 和 `scripts/unclassified.py` 接手——宁可空着,也不要一个
  假装是主题的动作词。

瘦身前后可以直接用 `/review` 顶部的「人工覆盖率」对比效果;`/api/topic_metrics` 的
`unused_standards` 应明显下降,`low_frequency_ratio` 同理。

## 鉴权提示

`/healthz` 提供**可观测性探活**（免登录），返回数据库连通、事件队列积压、死信数量、最近入库时间（数据新鲜度）：

```
GET /healthz -> {"status":"ok","db":true,"queue_backlog":0,"dead_letter":{"bytes":0,"lines":0},"last_message":"2026-10-26 09:58:51","last_message_seconds_ago":175192}
```

搭配告警脚本定时探活（服务器）：

```bash
# 手动试跑(dry-run 只打印不发)
./.venv/bin/python scripts/health_probe.py --dry-run

# 安装定时探活(每 5 分钟,异常推飞书告警)
cp deploy/feishu-health.service deploy/feishu-health.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now feishu-health.timer
systemctl list-timers feishu-health.timer
```

`/` 根路径已改为跳转总结页；旧的服务统计信息迁移到 `/api/stats`（需登录）。

```
GET /health -> {"status":"ok"}
```

## 变更历史

### 2026-11-06

- **标签指标页面 `/metrics`:把指标翻译成「现在该做什么」**
  - 新增 `app/static/metrics.{html,js,css}` + `GET /metrics`;`nav.js` 加入「标签指标」入口
  - `/api/topic_metrics` 扩展 `standard_coverage.labels`(**每个**标准标签的组数,含 0)与 `off_standard`(库内用过但不在标准集的标签):前者是「挂着没人用」的冗余,后者是「用了却不在册」的别名漏网,合起来就是体系漏洞视图
  - **顶部诊断横幅**是页面真正的产出:每个指标过阈值后汇成结论 + 该跑哪个脚本。`gold_groups=0` 时红色警示「所有准确率都不作数」;未分类率 ≥15% 起警 / ≥30% 转红;召回漏检率 ≥20% 提示调 `TOPIC_RECALL_TOP_K`
  - 6 个 KPI(未分类率 / 覆盖率 / 漏网数 / 待复核 / gold 规模 / 标签总数)+ 置信度分布 + 按大类分组的标签用量 + 归并与碎片
  - 进程内三项(归一化 / 召回 / 缝合)**卡片里明确写了「重启归零,只看趋势」**,否则每次部署后都会有人以为指标崩了
  - 接口 500 时页面不白屏,直接提示「多半是库缺 `topic_conf`/`stage` 列(代码没部署或没重启)」——最常踩的那个坑
  - 只读免 admin;30 秒自动刷新,切到后台暂停、切回来补一次
  - 新增 `tests/test_topic_metrics_page.py`(9 项)
- **标准体系瘦身:41 → 33,层级压平、动作词摘出、同义收敛**
  - 审计出三个结构病:**层级是反的**(18 个大组里 9 个其实是子组,「黑屏类问题」在充当分类,父组 `topics` 永远为空)、**动作词混进 topic**(`换机推送` 108 条 / `已推` 25 条 / `上门维修` 8 条 是 `stage` 不是现象)、**同义不收敛**(库内用过 62 个标签 vs 标准集 41 个)
  - 新体系 11 大类 / 33 个标准标签:画质族 6 合 1、语音族 3 合 1、动作类整体摘出;大类只作语境,现象统一粒度
  - 新增 `scripts/slim_topics.py`:默认预览不写库,`--apply` 才落库,`--restore <json>` 逐条还原;`validate_plan()` 校验不重复/无「类问题」后缀/目标必在新标准集/每个新标签都得有旧标签映射过来
  - **合并 N→1 不可逆 → 必须逐条快照**:`--apply` 前把每条 `messages.topic` / `conversations.topic` 原值写进 `data/topic_slim_backup-<ts>.json`;`--restore` 按 id 还原而不是靠映射反推(反推只能得到「最像的那个」,分不开原本三个标签的组)
  - **preflight 主动阻断**:缺 `messages.stage` 列时拒绝 `--apply`(缺 `topic_conf`/`topic_source` 只警告)——摘掉动作词等于丢掉唯一进度信号,必须先部署重启 + `backfill_stage.py`
  - 旧名沉淀进 `topic_aliases(auto=0)`,LLM 以后再输出旧名自动收敛,标签不会长回来;清空的 12 个标签(含 `换机推送` 108 条)回未分类队列交给 `/review` 接手
  - 合并会让两条知识库撞同一 topic(`投屏卡顿` / `镜像投屏卡顿`),`_knowledge_conflicts()` 只报不删
  - 新增 `tests/test_topic_slim.py`(15 项)
- **人工复核页面 `/review`:让闭环真的有人用**
  - 新增 `app/static/review.{html,js,css}` + `GET /review`:`nav.js` 加入「标签复核」入口
  - 队列卡片带样本文本、现值、置信度;输入框按大类分组提示标准标签,**回车即提交**;成功说出「旧值 → 新值 / 影响几条 / 学了哪个别名 / 连带回刷几条」并把卡片移出队列,失败原样保留并显示拒绝原因
  - 顶部统计直出 `pending_groups` / 未分类 / 低置信 / `gold_groups` / `human_rate`——`gold_groups=0` 时提醒「所有准确率都不作数」
  - **导出 CSV 与 `scripts/topic_review.py --export` 逐列一致**,且会带上「页面已填但未提交」的值,支持「页面批量填 → 导出 → 脚本 `--csv --apply`」
  - 非 admin 进页面直接只读态(横幅 + 禁用输入 + `只读` 按钮),不再填完才吃 403;提交遇 401 引导重新登录
  - 新增 `tests/test_topic_review.py::test_review_page_assets_exist`:三个静态文件都在、`review.html` 引用的资源真实存在、导航里有入口(单测断言,防「改了路径忘了建文件」)
- **人工复核闭环:低置信队列 -> 人工定标 -> 反哺别名 -> gold set**
  - 新增 `app/services/topic_review.py`:`list_review_queue`(未分类 + 低置信,未分类优先)、`apply_review`(回写三处 + 学别名 + 可选回刷)、`gold_stats` / `eval_gold`
  - 新增 `messages.topic_source`(`llm`/`human`),轻量迁移 `_ensure_stage_columns` 一并加列;人工确认的组不再进队列,并作为准确率评估基线
  - **两条硬护栏**:原标签是标准标签时绝不写别名(否则连锁改写整个标签体系);标签收敛不到标准集时拒绝且不改库(`--allow-new` 才放行)。`POST /api/topic_aliases` 同样加了标准标签判断——那是同一个 bug 的第二个入口
  - `TopicService` 抽出 `_decide_label`(只算不写库)+ 新增 `predict_group`:`--gold --eval` 与线上判别走同一条路径,否则量出来的不是线上的准确率
  - 新增 `scripts/topic_review.py`(队列/导出 csv/批量定标/预览 dry-run/gold 评估)与 `GET /api/review/queue`、`POST /api/review/apply`、`GET /api/review/gold`
  - `/api/topic_metrics` 新增 `review`(pending_groups / gold_groups / human_rate)
  - 新增 `tests/test_topic_review.py`(22 项)
- **候选召回:把「41 选 1」缩成「8 选 1」**
  - 新增 `app/services/topic_recall.py`:纯字面召回,三路打分取最大——精确命中(1.0)/ 覆盖度(标签 2-gram 被文本覆盖的比例,长文本下 Dice 会被稀释到接近 0 所以不能用)/ 历史样例 Dice × 0.9
  - 覆盖度会剥掉标签尾部的「类问题」这类空后缀(「画面模糊类问题」7 字里 3 字是后缀,不剥只得 0.33,剥掉 0.67)
  - **只窄化不放宽**:候选必是标准集子集;标签总数 ≤ top_k、`top_k=0`、全零信号一律不窄化;窄化后 LLM 判 `UNKNOWN` 用全集重问一次
  - 新增 `MessageRepo.get_label_exemplars`:单次查询按标签分桶取最近样例,避免 41 个标签 41 次查询
  - `filter_tree` 裁候选子树时保留「大类 > 子类」层级(大类名是语境),整类无候选的节点才摘掉;顺带修正未窄化时提示词误传 `None` 导致层级退化成平铺列表
  - 新增 `topic_recall_top_k`(默认 8)与 `recall` 指标(`narrow_rate` / `fallback_rate` = 召回漏检率);实测 41 标签提示词 511 → 227 字符
  - 新增 `tests/test_topic_recall.py`(13 项)
- **`5a9cf10` feat(ingest): 入库侧问题单元护栏——回复久无动静的旧组时新起一组**
  - `_resolve_root` 原来对回复消息无条件并入父消息所在组,「点着三天前的消息回复、问的却是全新问题」会把新问题挂进旧对话——这是畸形巨型对话唯一的**长出入口**,`split_cases.py` 只能事后修补
  - 新增配置 `case_split_gap_seconds`(默认 7200):整组**最后一条消息**距当前超过阈值即新起一组;判据用整组最后一条而非被回复那条(组内刚有人说话说明这轮还没结束);时间不可解析/`gap<=0` 按旧行为并入
  - 新增 `MessageRepo.group_last_create_time`(只取 `max(create_time)` 聚合,不加载整组);`_attach_conversation` 修正跨组边 `depth`(父不在本组按 1 记,否则抄父树深度会让根消息显示「深度 4」)
  - 只做物理新起一组,「是否仍属同一案例」交给 `conversations.case_id` 软关联(可解绑)
  - 新增 `tests/test_reply_gap_guard.py`(5 项)
- **`117e919` feat(topic): 未分类治理——相似度兜底 / 组内空标签回填 / 未分类清单**
  - 相似度兜底默认开启(`topic_alias_sim_threshold` 0 → 0.65):闭集要求输出标准标签原文,但常有 near-miss(「画面模糊」vs「画面模糊类问题」dice=0.67),不给兜底就被闭集判死成未分类;兜底后仍有闭集校验,映射不到只落回未分类,不会写脏标签
  - `TopicService.backfill_group_topic`:已打标组来了新消息只做零成本回填(补 `topic` + 同步 `conversations.topic`),不再调 LLM——修掉「组已有标签后新消息永远 topic=""」(14 组/40 条)与 `conversations.topic` 为空(19 组)两个存量不一致
  - 新增 `MessageRepo.list_unclassified_roots` / `count_unclassified_roots`:按组聚合、`having` 判定整组无标签,跨库可移植
  - 新脚本:`unclassified.py`(未分类清单 + 高频 2-gram)、`suggest_aliases.py`(非标准标签→标准标签 Dice 建议,`--apply` 才写别名表)、`backfill_group_topic.py`(存量回填,复用运行时同一方法)
  - `/api/topic_metrics` 新增 `unclassified` / `standard_coverage` / `confidence` 三组指标
  - 新增 `tests/test_topic_governance.py`(9 项)
- **`2e87cf2` feat(topic): 问题单元切分——把混了多个问题的对话切成一个问题一个对话**
  - 新增 `app/services/case_split.py`:确定性切点(换设备 `case_key` 变化 / `block_start` 标记 / 时间断档 > 2h),纯客套片并回上一片、图片片不当噪声;落地复用 `conversation_split.split_conversation`(计划精确覆盖校验、重挂消息、回复边迁移、统计重算),不重写拆库逻辑
  - 新增 `scripts/split_cases.py`:`--stats` 对比各阈值切分规模、默认 dry-run、`--apply` 落库、`--relabel` 立即重新归类
  - 落库后新单元 `topic`/`topic_conf` 清空、`stage` 按新组内文本重算,`keep` 组保留原标签;不继承 `case_id`(问题单元与案例组分离成两层)
  - `app/services/stage.py` 抽出公开 `is_filler_only` 供切分复用客套句判定
  - 切分后跨切点的回复边留在父消息所在对话,子消息两侧都按根渲染,不丢消息
- **`f4f2461` 话题分类:闭集判别 + 现象/阶段分维度**
  - topic 改为**闭集判别**:提示词把标准标签按「大类 > 子类」渲染成清单,LLM 只能在清单里选,选不出输出 `UNKNOWN`;闭集外的标签一律不写库、保留未分类(此前 23 组/100 条标签游离在标准集之外)
  - LLM 输出改为 JSON `{"topic","confidence"}`,`messages.topic_conf` 存自评置信度,供「高置信自动 / 中置信待复核 / 低置信转人工」分流;旧的纯文本格式仍兼容
  - 新增与 topic 正交的第二维度 **stage(处理阶段)**:`app/services/stage.py` 纯规则判定 待处理/处理中/已推送/已上门/已解决,把「已推」「上门维修」这类动作词从主题标签剥离;每条消息入库即重算,配 `scripts/backfill_stage.py` 回填存量
  - 轻量迁移:`messages.stage`/`messages.topic_conf`/`conversations.stage` 三列由 `db.py::_ensure_*` 查缺补列;`/api/summary` 返回 `stage`,总结页头部展示「主题 · x　阶段 · y」
  - 新增 `tests/test_topic_closed_set.py`(12 项)

### 2026-10-27

- **`eba1568` fix(front): /summary 恢复完整对话平铺展示与横杠分隔**
  - `139a56c` 前端工程化合并时 `summary.js` 取到旧版(递归回复树),`9955b0a` 的平铺+横杠逻辑丢失;移植回 `renderTimeline`:`flattenTree` 拍平后按时间升序,`block_start=1` 消息前画横杠(`.chat-sep`,深色主题 token)
  - 每条消息 who 行补回时间;保留新版 tabs/大类折叠/媒体画廊等功能
- **`139a56c` feat(front): 前端工程化——静态资源层 + 公共组件 + 全站深色 + 全局导航**(workbuddy 会话)
  - `summary_page.py`/`habits_page.py` 抽到 `app/static/`(summary.js/css、habits.js/css、components.css、nav.js),页面壳改为独立 html
  - 全站深色主题(设计 token 集中在 components.css)
- **`5377cf3` docs: 明确 COOKIE_SECURE 判定依据是浏览器侧协议而非应用监听协议**
- **`ae2dd3f` feat(auth): 登录注册与全站访问控制——未登录不可访问任何页面/接口**(workbuddy 会话)
  - users 表 + PBKDF2-SHA256(200k) 加盐哈希、HMAC-SHA256 签名 Stateless Cookie、5 次/5 分钟登录限流;`/login` 页面与 `/api/auth/*`;HTTP 中间件拦截(页面 302、API 401),`/static/` 与 `/feishu/event` 白名单放行
  - 建号脚本 `scripts/create_user.py`
- **存量数据修正(直接改生产库,无代码变更)**
  - 全量横杠回填:12 个对话新增 17 处 `block_start`(DB 共 20 处);判定=时间断档+发送者(不同门店/人=不同案例)+设备(UUID/MAC 不同=不同机器)+语义;同一案例续聊(如 #111 同一人 6 天追问、#146 隔夜复现)一律不画
  - 同主题同群对话归并 5 组:#317→#113 投屏卡顿、#304→#234 投屏找不到设备、#274→#146 系统不定时重启、#308→#63 花屏问题、#284→#144 镜像投屏卡顿(单条「好的」类续聊碎片用 `--no-block-start` 并入)
  - 大类配置追加 4 个 topic:画质模糊→画面模糊类问题、语音识别无跳转→语音类问题、取消推送更新+无法输入密码→系统类问题(加入即成为标准标签,后续同类 LLM 沿用、只归类不合并)
- **`e8df666` refactor: 会话缝合去掉 6 小时时间护栏**
  - 时间间隙不再是缝合护栏,是否同一案例完全交由 LLM 判定(隔夜续聊等长间隔也可自动合并);保留标签冲突护栏(双方 topic 不同不合并,已有确定标签是强反证)

- **`308ba72` fix: 回复树悬挂边子消息丢失;新增消息挪组脚本**
  - 回复边指向库外消息(回复了捕获前的旧消息、父消息不在 `messages` 表)时,子消息此前会无声地从回复树里丢失;现按根节点展示(线上案例:#317 的 16:54 消息)
  - 新增 `scripts/move_messages.py`:把某对话时间窗内的消息挪入另一对话(同一案例修正,回复边随动,双方重算统计)
- **`7c486ad` feat: 会话缝合后台周期调度(同一案例续聊自动合并)**
  - 新增 `app/services/conv_stitch_service.py`:后台守护线程每 `CONV_STITCH_INTERVAL_MINUTES`(默认 30 分钟)对水位线有变化的群跑 LLM 生命周期缝合,复用 `SchedulerLock` 租约多 worker 互斥;群水位线(会话数, 最大消息 id)不变则跳过省 LLM 调用
  - 横杠规则:缝合 = 同一问题处理生命周期续聊,视为同一问题不断句,不打 `block_start` 标记(前端不画横杠);同标签归并路径保留标记(不同案例画横杠),`merge_groups` 新增 `mark_block_start` 参数
  - 配置新增 `conv_stitch_enabled` / `conv_stitch_interval_minutes`;`main.py` lifespan 挂载启停
  - 附带 `scripts/merge_conversations.py`:手动把多个对话归并为一个(同一案例人工修正,支持 `--no-block-start` 不打横杠)
- **`9955b0a` feat: /summary 完整对话按时间顺序平铺展示,新问题块画横杠分隔**
  - `/summary` 完整对话不再按回复树嵌套,拍平后按 `create_time` 升序展示;`messages.block_start=1` 的消息前画横杠,分隔同一对话里的不同问题
  - 标记时机:带已有标签的新消息并入既有对话时(同标签归并/存量回填),由 `merge_groups` 自动打点;纯续聊不打
  - 附带 `scripts/backfill_block_start.py`:存量对话按最大时间断档回填 `block_start`(幂等)

### 更早

- `2a99d04` 分类提示词注入标准标签(问题大类预配置名),同类问题自动沿用大类标准名;标准名只归类不合并
- `433bd54` 问题大类支持两级分组(parent 父子层级),/summary 按 大类→子类→小类 三级折叠
- `a18c40c` 主题参考知识库(标准自查手册),AI 解决步骤按手册组织
- `69cdcd9` 问题大类分组(投屏问题=找不到设备+镜像卡顿+卡顿),/summary 按大类折叠小类

## 常见问题

- **收到事件但无消息入库**：确认机器人已被拉入该群，且应用权限被管理员审批通过。
- **回调地址校验失败**：飞书会先发一个 URL 校验请求（`challenge`），SDK 已自动处理；确认请求地址与 `event_path` 完全一致（`/feishu/event`）。
- **只想要特定群**：在 `.env` 填写 `ALLOWED_CHAT_IDS=oc_xxx,oc_yyy`。
- **忘记密码**：`python scripts/create_user.py 用户名 --reset-pwd 新密码`
- **页面显示「自助注册已关闭」**：库里已有账号且 `ALLOW_PUBLIC_REGISTER=false`,用 `create_user.py` 开号。
- **登录成功仍跳回登录页**：`COOKIE_SECURE` 与站点实际协议不匹配。https 站点却设了 `false`，或 http 站点却设了 `true`，浏览器都不会回传 cookie（后者连第一次 `Set-Cookie` 都会被丢弃）。
- **多实例部署反复掉线**：各实例 `SESSION_SECRET` 不一致,配成同一个值。

## 安全

- `config/.env` 含敏感凭据，已加入 `.gitignore`，**不要提交到仓库**。
- 消息内容未经加密直接落盘，注意存储权限。
- 全站默认要求登录（见「访问控制」）；`data/.session_secret` 是会话签名密钥，**泄露即可伪造任意用户的登录态**，须与 `.env` 同等对待（`data/` 已忽略）。
# feishu_message
