"""同主题归组:把同一群内相同 topic 标签的回复链组归并为一个对话。

背景:显式回复靠回复链归组、同人连发靠轮次合并归组;但「直接发言无回复」
的碎片反馈(如截图场景里连串追问)只能在 LLM 主题分类完成后按标签归并,
不依赖时间窗(有人半天才回复)。

归并语义:
- 方向:一律并入「更早的组」(原问题块保留为根,first_message_id 最小者为目标)
- 内容:messages 改 root_message_id/conversation_id,message_links 移入目标对话,
  源对话行删除,目标对话重算统计并同步 topic
- 幂等:源==目标、任一组无数据、已归并过均视为无操作
- 开关:settings.topic_merge_enabled=False 时只打标签不归并
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.models.conversation import MessageLink

logger = logging.getLogger(__name__)

# 归并互斥(主题分类可能由多个 worker 线程并发触发)
_merge_lock = threading.Lock()


def plan_merges(session: Session, chat_id: str, topic: str) -> list[tuple[str, str]]:
    """规划同群同 topic 的归并对,返回 [(source_root, target_root)],目标=最早组。

    只读不改库,供 dry-run 预览;少于 2 个组时返回空。
    """
    if not topic:
        return []
    repo = MessageRepo(session)
    roots = repo.list_topic_roots(chat_id, topic)  # [(root, first_id)] 最早在前
    if len(roots) < 2:
        return []
    target = roots[0][0]
    return [(root, target) for root, _fid in roots[1:]]


def merge_groups(
    session: Session,
    chat_id: str,
    source_root: str,
    target_root: str,
    topic: str = "",
    mark_block_start: bool = True,
) -> bool:
    """把 source 组整体并入 target 组,返回是否发生归并。

    source/target 任一组无数据、或两者相同,返回 False(幂等)。
    mark_block_start=False 时不打新问题块起点标记(多对话实为同一案例时使用)。
    """
    if not source_root or not target_root or source_root == target_root:
        return False
    repo = MessageRepo(session)
    convo = ConversationRepo(session)

    source_rows = repo.list_by_root(source_root, chat_id)
    target_rows = repo.list_by_root(target_root, chat_id)
    if not source_rows or not target_rows:
        return False

    # 目标对话缺失时补建(历史数据未跑 conversations 回填的兜底)
    target_conv = convo.get(chat_id, target_root)
    if target_conv is None:
        target_row = next((m for m in target_rows if m.message_id == target_root), None)
        target_conv = convo.create(
            chat_id, target_root, target_row.sender_id if target_row else ""
        )
        session.flush()

    source_conv = convo.get(chat_id, source_root)

    with _merge_lock:
        # 1) 消息改挂目标组
        for m in source_rows:
            m.root_message_id = target_root
            m.conversation_id = target_conv.id
        # 1b) 新消息带着已有标签并入既有对话 → 标记新问题块起点(首条消息),
        #     前端在完整对话里于该消息前画横杠;无标签(空)不标记
        if mark_block_start and any((m.topic or "").strip() for m in source_rows):
            first_row = min(
                source_rows,
                key=lambda r: (
                    int(r.create_time) if (r.create_time or "").isdigit() else 0,
                    r.id,
                ),
            )
            first_row.block_start = 1
        # 2) 源对话的回复边移入目标对话(树在目标对话下呈现多个根节点)
        if source_conv is not None:
            session.execute(
                update(MessageLink)
                .where(MessageLink.conversation_id == source_conv.id)
                .values(conversation_id=target_conv.id)
            )
            # 3) 删除源对话行(唯一键 chat+root 释放)
            session.delete(source_conv)
        # 4) 目标对话补 topic + 重算统计
        if topic and not target_conv.topic:
            target_conv.topic = topic
        convo.update_group_stats(target_conv.id)
        session.commit()

    logger.info(
        "同主题归组 %s: %s -> %s (%d 条, topic=%s)",
        chat_id,
        source_root,
        target_root,
        len(source_rows),
        topic or "-",
    )
    return True


def merge_topic_group(session: Session, chat_id: str, topic: str) -> list[tuple[str, str]]:
    """同群内相同 topic 的全部组归并到最早组,返回实际归并的 (源, 目标) 列表。

    开关关闭或无可归并组时返回空列表。
    """
    if not get_settings().topic_merge_enabled or not topic:
        return []
    done: list[tuple[str, str]] = []
    for source, target in plan_merges(session, chat_id, topic):
        if merge_groups(session, chat_id, source, target, topic):
            done.append((source, target))
    return done
