"""消息业务服务。

职责:接收飞书事件回调,解析出结构化消息,并通过 DAO 落库。
含防重(chat_id+message_id)与群白名单过滤。

分组:A 发消息、B 回复 A(事件带 parent_id/root_id),两者归为同一组,
组内共享 root_message_id(回复链的根消息 id)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.dao.message_repo import MessageRepo
from app.dao.conversation_repo import ConversationRepo
from app.models.message import Message
from app.schemas.message import MessageCreate
from app.services.image_service import get_image_service
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

logger = logging.getLogger(__name__)

# 支持解析的内容类型
_TEXT_TYPES = {"text", "post"}


class MessageService:
    def __init__(
        self,
        session: Session,
        allowed_chat_ids: set[str] | None = None,
        topic_scheduler: Callable[[str, str], None] | None = None,
    ):
        self._session = session
        self._allowed = allowed_chat_ids or set()
        self._repo = MessageRepo(session)
        self._convo = ConversationRepo(session)
        # 入库后回调 (chat_id, root_message_id),用于调度异步主题分类
        self._topic_scheduler = topic_scheduler

    # ---------------------------------------------------------------- 对外
    def handle_receive_event(self, data: P2ImMessageReceiveV1) -> bool:
        """处理一条 im.message.receive_v1 事件。返回是否成功入库。"""
        event = data.event
        if event is None or event.message is None:
            logger.warning("收到空消息事件,忽略")
            return False

        msg = event.message
        chat_id = msg.chat_id or ""

        # 群白名单过滤
        if self._allowed and chat_id not in self._allowed:
            logger.info("跳过非白名单群 chat_id=%s", chat_id)
            return False

        # 解析发送者
        sender_id, sender_type = _sender_info(event.sender)

        # 解析消息内容
        text, raw = _parse_content(msg.message_type or "", msg.content or "")

        # 图片消息:把 image_key 下载上传七牛,生成可展示 URL 存入 raw.image_url。
        # 未配置七牛/下载失败则优雅降级(保留 image_key,下次进群消息仍会尝试)。
        if (msg.message_type or "") == "image" and isinstance(raw, dict):
            raw.setdefault("image_url", "")
            image_key = raw.get("image_key") or ""
            if image_key:
                try:
                    raw["image_url"] = get_image_service().resolve_image_url(
                        image_key, msg.message_id or ""
                    )
                except Exception:  # noqa: BLE001 - 图片处理失败不影响入库
                    logger.warning("处理图片消息异常 image_key=%s", image_key, exc_info=True)


        # 解析回复关系(仅回复消息带 parent_id / root_id)
        parent_id = getattr(msg, "parent_id", None) or ""
        root_id = getattr(msg, "root_id", None) or ""

        # 解析 @ 提及:飞书回调 text 里是占位符(如 @_user_1),真实名字在 msg.mentions[i].name。
        # 记录 key->真实名 映射,供展示/LLM 时把占位符替换成 "@真实姓名"。
        mentions = _extract_mentions(getattr(msg, "mentions", None) or [])
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raw = {"raw_content": raw}
        if mentions:
            raw["mentions"] = mentions

        create = MessageCreate(
            message_id=msg.message_id or "",
            chat_id=chat_id,
            sender_id=sender_id,
            sender_type=sender_type,
            message_type=msg.message_type or "",
            text=text,
            # SDK 将 create_time 解析为 int,统一转 str 落库
            create_time=str(getattr(msg, "create_time", None) or ""),
            parent_message_id=parent_id,
            root_message_id=root_id,
            raw=raw,
        )

        return self.save_message(create)

    def save_message(self, create: MessageCreate) -> bool:
        """保存一条解析后的消息(含对话关系写入)。返回是否新写入。"""
        return self._save(create)

    def list_groups(self, chat_id: str | None = None, limit: int = 50) -> list[dict]:
        """按回复链分组返回最近的消息组。

        每组: {root_message_id, chat_id, root, replies, message_count, senders}。
        独立消息自成一组;A 发消息 B 回复,归为同一组。
        """
        groups: list[dict] = []
        for root_id, group_chat in self._repo.list_roots(
            chat_id=chat_id, limit=limit
        ):
            rows = self._repo.list_by_root(root_id, group_chat)
            root_row = next((m for m in rows if m.message_id == root_id), None)
            replies = [m for m in rows if m.message_id != root_id]
            senders: list[str] = []
            for m in rows:
                if m.sender_id and m.sender_id not in senders:
                    senders.append(m.sender_id)
            groups.append(
                {
                    "root_message_id": root_id,
                    "chat_id": group_chat,
                    "root": root_row,
                    "replies": replies,
                    "message_count": len(rows),
                    "senders": senders,
                }
            )
        return groups

    # ---------------------------------------------------------------- 内部
    def _resolve_root(self, create: MessageCreate) -> str:
        """确定消息所属分组的根消息 id。

        - 独立消息(无 parent):根=自身
        - 回复消息:优先取父消息已落库的 root(保证多级回复归到同一根);
          父消息未采集到(如机器人入群前)时,回退用事件 root_id,再回退用 parent_id。
        """
        if not create.parent_message_id:
            return create.message_id

        parent = self._repo.get_by_message_id(
            create.chat_id, create.parent_message_id
        )
        if parent is not None:
            return parent.root_message_id or parent.message_id
        # 父消息不在库中:用事件携带的 root_id,否则以被回复消息为分组锚点
        return create.root_message_id or create.parent_message_id

    def _save(self, create: MessageCreate) -> bool:
        """落库并防重。返回 True 表示新写入,False 表示重复跳过。"""
        # 防重:同一 chat+message 不重复入库
        if self._repo.exists(create.chat_id, create.message_id):
            logger.debug("重复消息跳过 %s/%s", create.chat_id, create.message_id)
            return False

        msg = Message(
            message_id=create.message_id,
            chat_id=create.chat_id,
            sender_id=create.sender_id,
            sender_type=create.sender_type,
            message_type=create.message_type,
            text=create.text,
            create_time=create.create_time,
            parent_message_id=create.parent_message_id,
            root_message_id=self._resolve_root(create),
            raw=create.raw,
        )
        try:
            self._repo.insert(msg)
            self._attach_conversation(msg)
            logger.info(
                "消息入库 [%s] %s @ %s (%s): %s",
                create.message_id,
                create.sender_id,
                create.chat_id,
                create.message_type,
                create.text[:80],
            )
            # 异步调度主题分类(不阻塞入库)
            if self._topic_scheduler is not None:
                try:
                    self._topic_scheduler(create.chat_id, msg.root_message_id)
                except Exception:  # noqa: BLE001
                    logger.exception("调度主题分类失败(不影响入库)")
            return True
        except Exception:  # noqa: BLE001  (唯一索引竞态等)
            self._session.rollback()
            # 竞态导致的重复,直接忽略
            if self._repo.exists(create.chat_id, create.message_id):
                logger.debug("并发重复消息,忽略")
                return False
            logger.exception("消息入库失败 %r", create)
            raise

    def _attach_conversation(self, msg: Message) -> None:
        """把消息挂接到对话表,并维护回复边与对话聚合统计。

        - 确保 conversations 存在(按 chat_id+root_message_id)
        - 设置 msg.conversation_id
        - 回复消息则写一条 message_links 边(含 depth)
        - 重算对话的 message_count / participant_count / 时间
        """
        root = msg.root_message_id or msg.message_id
        conv = self._convo.get(msg.chat_id, root)
        if conv is None:
            # 根消息创建对话;非根消息(根未被采集到)暂用当前消息做锚点
            root_sender = msg.sender_id if (msg.message_id == root) else ""
            conv = self._convo.create(msg.chat_id, root, root_sender)

        msg.conversation_id = conv.id

        # 回复消息 → 写回复边(子=当前消息, 父=parent_message_id)
        if msg.parent_message_id:
            parent_depth = self._convo.get_depth(msg.parent_message_id)
            depth = parent_depth + 1 if parent_depth >= 0 else 1
            parent_sender = ""
            parent = self._repo.get_by_message_id(msg.chat_id, msg.parent_message_id)
            if parent is not None:
                parent_sender = parent.sender_id
            self._convo.add_link(
                conv.id,
                parent_message_id=msg.parent_message_id,
                child_message_id=msg.message_id,
                parent_sender_id=parent_sender,
                child_sender_id=msg.sender_id,
                depth=depth,
            )

        # 重算聚合统计
        self._convo.update_group_stats(conv.id)
        self._session.commit()


# ---------------------------------------------------------------- 解析工具
def _sender_info(sender: Any) -> tuple[str, str]:
    if sender is None:
        return "", ""
    s = getattr(sender, "sender_id", None)
    sid = ""
    if s is not None:
        sid = (
            getattr(s, "open_id", None)
            or getattr(s, "user_id", None)
            or getattr(s, "union_id", None)
            or ""
        )
    stype = getattr(sender, "sender_type", None) or ""
    return sid, stype


def _extract_mentions(mention_events: list) -> dict[str, str]:
    """从飞书消息事件的 mentions 数组提取 {占位符key: 真实姓名} 映射。

    占位符如 "@_user_1";name 即真实姓名(如 "王国浩")。@所有人等非用户场景 name 为空。
    """
    mapping: dict[str, str] = {}
    for m in mention_events or []:
        key = getattr(m, "key", None)
        name = getattr(m, "name", None) or ""
        if key:
            mapping[key] = name
    return mapping


def render_mentions_text(text: str, mentions: dict | None) -> str:
    """把文本里的 @占位符(如 @_user_1)替换成 @真实姓名。

    mentions 形如 {"@_user_1": "王国浩"}。未配置映射时原样返回。
    """
    if not text or not mentions:
        return text
    for key, name in mentions.items():
        if name:
            text = text.replace(key, f"@{name}")
    return text


def _parse_content(msg_type: str, content_json: str) -> tuple[str, dict[str, Any] | None]:
    """解析 content 为 (可读文本, 原始结构化数据)。

    支持 text / post(富文本)。图片/文件/语音等保留原始数据,文本为空。
    """
    try:
        raw: Any = json.loads(content_json) if content_json else {}
    except json.JSONDecodeError:
        logger.warning("消息 content 非合法 JSON: %r", content_json[:200])
        return "", {"raw_content": content_json}

    if not isinstance(raw, dict):
        return "", {"raw_content": content_json}

    if msg_type == "text":
        return (raw.get("text") or "").strip(), raw
    if msg_type == "post":
        text = _extract_post_text(raw.get("content"))
        return text, raw
    return "", raw


def _extract_post_text(content: Any) -> str:
    """从飞书富文本 post 提取纯文本。"""
    parts: list[str] = []
    if isinstance(content, list):
        for section in content:
            if isinstance(section, list):
                for run in section:
                    if isinstance(run, dict) and run.get("text"):
                        parts.append(run["text"])
    return " ".join(parts).strip()
