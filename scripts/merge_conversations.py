#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动归并脚本:把多个对话归并为一个(同一案例的人工修正)。

用法:
    python scripts/merge_conversations.py --ids 317,318,319 --topic 投屏卡顿 [--no-block-start] [--dry-run]

- 以最早首条消息的对话为目标,其余对话依次并入(保留根消息,源对话删除)
- --topic: 归并后统一给对话与全部消息打该标签(缺省保持目标原标签)
- --no-block-start: 不打新问题块起点标记(多个对话实为同一案例,前端不画横杠)
- 幂等:已归并过的对话再跑视为无操作;--dry-run 只打印计划
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal  # noqa: E402
from app.dao.conversation_repo import ConversationRepo  # noqa: E402
from app.dao.message_repo import MessageRepo  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.group_merge import merge_groups  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="手动把多个对话归并为一个")
    parser.add_argument("--ids", required=True, help="逗号分隔的对话 id 列表")
    parser.add_argument("--topic", default="", help="归并后统一打的话题标签")
    parser.add_argument("--no-block-start", action="store_true", help="不打新问题块起点标记")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划,不改库")
    args = parser.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    if len(ids) < 2:
        print("至少需要 2 个对话 id")
        return 1

    with SessionLocal() as s:
        convo = ConversationRepo(s)
        repo = MessageRepo(s)
        convs = [s.get(Conversation, cid) for cid in ids]
        missing = [cid for cid, c in zip(ids, convs) if c is None]
        if missing:
            print(f"对话不存在: {missing}")
            return 1

        chats = {c.chat_id for c in convs}
        if len(chats) > 1:
            print(f"对话分属不同群 {chats}, 拒绝归并")
            return 1
        chat_id = chats.pop()

        def first_ts(c):
            rows = s.query(Message).filter(Message.conversation_id == c.id).all()
            times = [int(m.create_time) for m in rows if (m.create_time or "").isdigit()]
            return min(times) if times else 0

        target = min(convs, key=first_ts)
        sources = [c for c in convs if c.id != target.id]
        print(f"群: {chat_id}")
        print(f"目标(最早): #{target.id} topic={target.topic!r} 根={target.root_message_id}")
        for c in sources:
            print(f"  并入: #{c.id} topic={c.topic!r} msgs={c.message_count} 根={c.root_message_id}")
        print(f"标签: {args.topic or '(保持目标原标签)'}  打横杠标记: {'否' if args.no_block_start else '是'}")
        if args.dry_run:
            print("dry-run, 未改库")
            return 0

        for c in sorted(sources, key=first_ts):
            ok = merge_groups(
                s, chat_id, c.root_message_id, target.root_message_id,
                topic=args.topic, mark_block_start=not args.no_block_start,
            )
            print(f"归并 #{c.id} -> #{target.id}: {'完成' if ok else '无需操作'}")

        if args.topic:
            target_conv = s.get(Conversation, target.id)
            if target_conv and target_conv.topic != args.topic:
                target_conv.topic = args.topic
            n = repo.set_topic_for_group(chat_id, target.root_message_id, args.topic)
            s.commit()
            print(f"统一标签 {args.topic!r}: 消息更新 {n} 条, 对话 #{target.id}")
        else:
            s.commit()

        final = s.get(Conversation, target.id)
        print(f"结果: #{final.id} topic={final.topic!r} msgs={final.message_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
