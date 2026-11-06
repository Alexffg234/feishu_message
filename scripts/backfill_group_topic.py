#!/usr/bin/env python3
"""回填「组内空标签」与 conversations.topic(零 LLM,幂等)。

两个存量不一致,都是审计查出来的:
1. 已打标组之后到达的消息永远 topic="":旧调度见「组已有标签」就直接跳过,
   这批消息从未被回填(14 组 / 40 条);
2. conversations.topic 为空但组内消息有标签(19 组),页面读到恒为空。

运行时已修(schedule_group 现在会先做零成本回填),本脚本处理历史存量。
回填逻辑与运行时完全一致:直接复用 TopicService.backfill_group_topic。

用法:
    python scripts/backfill_group_topic.py --dry-run     # 只统计,不写库
    python scripts/backfill_group_topic.py               # 全量回填
    python scripts/backfill_group_topic.py --chat oc_xxx # 只处理某群
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import case, func, select  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.topic_service import TopicService  # noqa: E402


def _groups_with_topic(session, chat: str = "") -> list[tuple[str, str, str, int]]:
    """返回 [(chat_id, root_message_id, topic, 组内空标签条数)],仅含有标签的组。"""
    max_topic = func.max(Message.topic)
    empty_cnt = func.sum(case((Message.topic == "", 1), else_=0))
    stmt = (
        select(Message.chat_id, Message.root_message_id, max_topic, empty_cnt)
        .where(Message.root_message_id != "")
        .group_by(Message.chat_id, Message.root_message_id)
        .having(max_topic != "")
    )
    if chat:
        stmt = stmt.where(Message.chat_id == chat)
    return [
        (r[0], r[1], r[2] or "", int(r[3] or 0)) for r in session.execute(stmt).all()
    ]


def _conv_topic_missing(session, chat: str = "") -> int:
    stmt = select(func.count(Conversation.id)).where(Conversation.topic == "")
    if chat:
        stmt = stmt.where(Conversation.chat_id == chat)
    return int(session.execute(stmt).scalar() or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="回填组内空标签与 conversations.topic")
    parser.add_argument("--chat", default="", help="只处理指定 chat_id")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        groups = _groups_with_topic(session, args.chat)
        dirty = [g for g in groups if g[3] > 0]
        print(
            f"有标签的组 {len(groups)} 个,其中组内还有空标签的 {len(dirty)} 个,"
            f"共 {sum(g[3] for g in dirty)} 条空标签消息"
        )
        print(
            f"conversations.topic 为空的对话 {_conv_topic_missing(session, args.chat)} 个"
        )
        if args.dry_run:
            for chat_id, root, topic, missing in dirty[:20]:
                print(f"  {chat_id} {root} -> {topic!r} 缺 {missing} 条")
            return 0

    # 空标签为 0 但 conversations.topic 可能仍为空:一并交给回填方法处理
    svc = TopicService()
    targets = [(c, r) for c, r, _t, _n in groups]
    ok = 0
    for i, (chat_id, root) in enumerate(targets, start=1):
        if svc.backfill_group_topic(chat_id, root):
            ok += 1
        if i % 100 == 0:
            print(f"  处理 {i}/{len(targets)}")

    with SessionLocal() as session:
        print(
            f"已处理 {ok}/{len(targets)} 组;剩余 conversations.topic 为空的对话 "
            f"{_conv_topic_missing(session, args.chat)} 个"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
