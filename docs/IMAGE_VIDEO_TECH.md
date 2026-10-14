# 飞书群聊图片/视频获取与存储技术文档

> 适用于 feishu_group_message 项目。本文档完整说明「群聊中的图片、视频」是如何被采集、下载、持久化并提供长期可访问 URL 的。

---

## 1. 问题背景

飞书开放平台的**资源类消息**（图片、视频、文件、音频）和纯文本不同：

- 事件回调 `im.message.receive_v1` 的 `content` 字段里，**并不是图片/视频的二进制或可直接访问的 URL**，而只是一串**资源 key（临时凭证）**。
- 这些 key（`image_key` / `file_key`）**有时效、会过期**，过期后无法再取回资源。
- 因此**必须在收到消息的当下**（key 还有效时）把资源下载下来，转存到**长期稳定的对象存储（本系统用七牛 Qiniu）**，生成永久可访问的 URL。

---

## 2. 飞书资源消息的数据结构

### 2.1 图片消息（消息类型 `image`）

事件回调 `event.message.content` 示例：

```json
{
  "image_key": "img_v3_02a5_xxxx"
}
```

- 只有一个 `image_key`，用于唯一标识图片资源。

### 2.2 视频消息（消息类型 `media`）

```json
{
  "file_key": "file_v3_02a5_xxxx",
  "file_name": "demo.mp4",
  "image_key": "img_v3_02a5_yyyy",
  "duration": 71033
}
```

- `file_key`：视频本体资源标识
- `image_key`：视频**封面图**（本质是一张图片）
- `file_name` / `duration`：辅助元信息

> 注意：同一个 `media` 消息里有两个 key——视频本体 + 封面图，两者要分别下载、分别存储。

---

## 3. 核心原理：两个关键接口

### 3.1 飞书「获取消息中的资源文件」接口（下载源）

```
GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={res_type}
```

| 参数 | 说明 |
|------|------|
| `message_id` | 消息 ID，**必须传**，否则报错 |
| `file_key` | 资源 key（图片/视频的 key） |
| `type` | `image`（图片/封面）或 `file`（视频/文件/音频） |

**关键点（易踩坑）：**

- ⚠️ 下载用户发的资源**必须走这个 `resources` 接口**，并带上 `message_id`。
- ⚠️ **不要**用 `/im/v1/images/{image_key}`（那个接口是给机器人**自己上传**的图片用的，用来下载用户资源会返回错误码 **234001 Invalid request param**）。
- 返回的是资源的**二进制流**（`resp.file`，可直接 `.read()` 成 `bytes`）。

### 3.2 七牛对象存储（长期存储）

下载到二进制后，上传到七牛 bucket，生成可访问 URL：

```
https://{qiniu_domain}/{qiniu}/{images|files}/{file_key}
```

- key 直接用飞书的 `file_key`，天然唯一、幂等、可对回原资源。
- 图片存 `images/` 目录，视频存 `files/` 目录。

---

## 4. 系统架构与流程

```
群聊成员发图片/视频
        │
        ▼
飞书服务器  ──事件推送──►  /feishu/event (FastAPI)
        │                      │
        │                      ▼
        │           lark-oapi dispatcher → 校验签名/解密
        │                      │
        │                      ▼
        │         MessageService.handle_receive_event()
        │                      │
        │              解析 content → raw
        │                      │
        │        ┌─────────────┴──────────────┐
        │        │ 图片 image                 │ 视频 media
        │        │ _resolve_image()          │ _resolve_video()
        │        │  image_key                │  file_key(本体) + image_key(封面)
        │        └─────────────┬──────────────┘
        │                      ▼
        │           ImageService 下载 + 上传七牛
        │           1) 飞书 resources 接口下载二进制
        │           2) 上传 qiniu → 得到 image_url/file_url
        │                      │
        │                      ▼
        │             写入 raw.image_url / raw.file_url
        │                      │
        ▼                      ▼
                         落库 MySQL(messages.raw)
```

### 4.1 代码调用链

1. `app/services/message_service.py` → `handle_receive_event()`
2. 对 `image` 消息调用 `_resolve_image(raw, message_id)`
3. 对 `media` 消息调用 `_resolve_video(raw, message_id)`
4. 二者内部都委托给 `app/services/image_service.py` 的 `ImageService`：
   - `resolve_image_url(image_key, message_id)` → 下载 + 上传 → 返回 URL
   - `resolve_file_url(file_key, message_id)` → 下载 + 上传 → 返回 URL
5. 得到的 URL 写回 `raw["image_url"]` / `raw["file_url"]`，随消息一起入库。

### 4.2 落库字段（存储在 `messages.raw` JSON 列）

| 消息类型 | 字段 | 含义 |
|----------|------|------|
| image | `image_key` | 飞书原始凭证（临时） |
| image | `image_url` | 七牛可展示 URL（永久） |
| media | `file_key` / `file_name` / `duration` | 视频元信息 |
| media | `file_url` | 视频本体七牛 URL |
| media | `image_key` / `image_url` | 视频封面（image_url 为七牛封面 URL） |

---

## 5. 配置（config/.env）

在服务器 `config/.env` 配置七牛（缺任一配置则资源消息**保留 key、优雅降级不报错**）：

```dotenv
# 七牛对象存储
QINIU_ACCESS_KEY=你的AccessKey
QINIU_SECRET_KEY=你的SecretKey
QINIU_BUCKET=你的存储空间名
QINIU_DOMAIN=https://cdn.你的域名        # 七牛 CDN 访问域名,不带尾斜杠

# 图片资源下载开关
IMAGE_DOWNLOAD_ENABLED=true
```

配置读取位于 `app/core/config.py`（`image_download_enabled`、`qiniu_*`），通过 `Settings.image_enabled` 属性统一判断是否启用。

---

## 6. 权限要求

需要飞书应用的以下权限（在飞书开放平台「权限管理」申请并发布）：

- `im:resource` —— **读取消息中的文件资源**（下载图片/视频必须）

> 机器人还必须**在目标群内**，且应用已发布生效。

---

## 7. 优雅降级设计

本项目的一个设计原则：**资源获取失败绝不能阻塞消息入库**。

- 未配置七牛 → 跳过下载，保留 `image_key` / `file_key`
- 飞书无下载权限 / 网络异常 / 下载失败 → 记日志、返回空串，消息照常入库
- 因此即便资源获取没打通，文本和消息结构依然完整，只是 `image_url` / `file_url` 为空（前端显示"未落盘"占位）。

---

## 8. 历史消息回补（幂等）

机器人入群前或当时没配上七牛的资源消息，可能没落盘 URL。可用回填脚本补拉：

```bash
# 服务器上,务必用项目 venv
./.venv/bin/python scripts/backfill_images.py --limit 500
```

- 扫描 `message_type` 为 `image` / `media` 且 `image_url` / `file_url` 为空的记录
- 重新下载 + 上传七牛 + 回写 URL
- **幂等**：可重复运行，已落盘的不会重复处理
- 可选参数：`--chat oc_xxx`（指定群）、`--dry-run`（只预览不写库）

---

## 9. 前端展示

- 列表 / 总结接口的树节点都带有：
  - `image_url` / `file_url`：已落盘的可访问 URL
  - `image_key` / `file_key`：原始飞书凭证
- 图片用 `<img>` 展示；视频用 `<video>`（`poster` 用封面 `image_url`）。

---

## 10. 常见问题排查

| 现象 | 原因 / 排查 |
|------|------------|
| 图片能收到消息但 `image_url` 为空 | 七牛未配置、或 `im:resource` 权限未开通、或下载失败（看日志 `WARNING`） |
| 下载报错 **234001** | 用了 `/im/v1/images/{image_key}` 快捷接口；应改用 `resources` 接口并带 `message_id` |
| `file_url` 为空但 `image_url` 有值 | 视频本体下载失败（`type=file`），封面（`type=image`）成功 |
| 历史图片没有 URL | 跑 `backfill_images.py` 回补 |
| 服务器跑脚本报 `ModuleNotFoundError: sqlalchemy` | 用了系统 `python` 而非项目 venv，应使用 `./.venv/bin/python` |

---

## 11. 相关文件

| 文件 | 职责 |
|------|------|
| `app/services/image_service.py` | 下载 + 上传七牛，产出可展示 URL 的核心服务 |
| `app/services/message_service.py` | 消息入库时调用 `_resolve_image` / `_resolve_video` |
| `app/core/config.py` | 七牛 / 开关配置 |
| `scripts/backfill_images.py` | 历史资源消息回补 |
| `app/dao/conversation_repo.py` | 输出 `image_url` / `file_url` 等字段 |
