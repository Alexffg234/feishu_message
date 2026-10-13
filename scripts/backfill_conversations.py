#!/usr/bin/env python3
"""回填脚本:把存量消息的回复关系转成 conversations + message_links 两表。

背景:新版引入独立对话表后,存量 messages 表(已有 parent_message_id/root_message_id)
还未生成 conversations 与 message_links。本脚本一次性回填,幂等可重复运行。

用法:
    python scripts/backfill_conversations.py [--chat oc_xxx] [--limit 1000]
    python scripts/backfill_conversations.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.dao.conversation_repo import ConversationRepo  # noqa: E402
from app.dao.message_repo import MessageRepo  # noqa: E402
from app.models.message import Message  # noqa: E402

logger = logging.getLogger("backfill_conversations")


def _roots(repo: MessageRepo, chat_id: str | None, limit: int) -> list:
    """取需要回填的分组(有 root_message_id 的消息所属的所有 root)。"""
    from sqlalchemy import func

    stmt = (
        select(Message.root_message_id, Message.chat_id, func.count(Message.id))
        .where(Message.root_message_id != "")
        .group_by(Message.root_message_id, Message.chat_id)
        .order_by(func.max(Message.id).desc())
        .limit(limit)
    )
    if chat_id:
        stmt = stmt.where(Message.chat_id == chat_id)
    return list(SessionLocal().execute(stmt).all())


def backfill(dry_run: bool, chat_id: str | None, limit: int) -> int:
    """回填对话关系,返回处理的 root 组数。"""
    with SessionLocal() as session:
        repo = MessageRepo(session)
        convo = ConversationRepo(session)
        roots = _roots(repo, chat_id, limit)
        processed = 0

        for root_id, root_chat, _n in roots:
            rows = repo.list_by_root(root_id, root_chat)
            if not rows:
                continue
            # 1) 确保 conversation 存在
            conv = convo.get(root_chat, root_id)
            if conv is None:
                root_row = next((m for m in rows if m.message_id == root_id), None)
                conv = convo.create(
                    root_chat,
                    root_id,
                    root_row.sender_id if root_row else "",
                )

            # 2) 挂接消息 conversation_id(TODO: update_group_stats 兜底)
            for m in rows:
                if m.conversation_id != conv.id:
                    m.conversation_id = conv.id
                    session.add(m)

            # 3) 写回复边
            for m in rows:
                if not m.parent_message_id:
                    continue
                parent_depth = convo.get_depth(m.parent_message_id)
                depth = parent_depth + 1 if parent_depth >= 0 else 1
                parent = repo.get_by_message_id(m.chat_id, m.parent_message_id)
                convo.add_link(
                    conv.id,
                    parent_message_id=m.parent_message_id,
                    child_message_id=m.message_id,
                    parent_sender_id=parent.sender_id if parent else "",
                    child_sender_id=m.sender_id,
                    depth=depth,
                )

            # 4) 重算聚合统计
            convo.update_group_stats(conv.id)
            processed += 1
            if processed % 100 == 0:
                logger.info("已处理 %d 组", processed)

        if dry_run:
            session.rollback()
            print(f"[dry-run] 将处理 {processed} 个对话组(未写入)")
        else:
            session.commit()
            print(f"回填完成: 处理 {processed} 个对话组")
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="回填存量对话关系到 conversations/message_links")
    parser.add_argument("--chat", default=None, help="只回填指定 chat_id(可选)")
    parser.add_argument("--limit", type=int, default=100000, help="最多处理多少分组(默认全部)")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    print(f"数据库: {settings.database_url}")
    backfill(dry_run=args.dry_run, chat_id=args.chat, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
