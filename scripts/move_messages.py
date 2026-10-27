#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消息挪组脚本:把某对话中指定时间窗内的消息挪到另一个对话(同一案例修正)。

用法:
    python scripts/move_messages.py --from 143 --to 317 \
        --start "2026-10-26 16:50" --end "2026-10-26 17:20" [--topic 投屏卡顿] [--dry-run]

- 源对话中 create_time 落在 [start, end] 的消息整体挪入目标对话
  (改 root_message_id/conversation_id,随带的回复边同步移走)
- --topic: 挪入消息统一打该标签(缺省不打)
- 不打 block_start 标记(同一案例,不画横杠)
- 挪后双方重算统计;--dry-run 只打印计划
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal  # noqa: E402
from app.dao.conversation_repo import ConversationRepo  # noqa: E402
from app.models.conversation import Conversation, MessageLink  # noqa: E402
from app.models.message import Message  # noqa: E402


def _ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description="把某对话时间窗内的消息挪到另一个对话")
    parser.add_argument("--from", dest="src_id", required=True, type=int, help="源对话 id")
    parser.add_argument("--to", dest="tgt_id", required=True, type=int, help="目标对话 id")
    parser.add_argument("--start", required=True, help='"YYYY-MM-DD HH:MM" 窗起点')
    parser.add_argument("--end", required=True, help='"YYYY-MM-DD HH:MM" 窗终点')
    parser.add_argument("--topic", default="", help="挪入消息统一打的标签")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划,不改库")
    args = parser.parse_args()

    lo, hi = _ms(args.start), _ms(args.end)

    with SessionLocal() as s:
        src = s.get(Conversation, args.src_id)
        tgt = s.get(Conversation, args.tgt_id)
        if src is None or tgt is None:
            print("源/目标对话不存在")
            return 1
        if src.chat_id != tgt.chat_id:
            print("源/目标分属不同群,拒绝")
            return 1

        rows = (
            s.query(Message)
            .filter(Message.conversation_id == src.id)
            .all()
        )
        moved = [
            m for m in rows
            if (m.create_time or "").isdigit() and lo <= int(m.create_time) <= hi
        ]
        if not moved:
            print("窗口内没有消息,无操作")
            return 0

        print(f"群: {src.chat_id}")
        print(f"源 #{src.id} topic={src.topic!r}  目标 #{tgt.id} topic={tgt.topic!r}")
        for m in sorted(moved, key=lambda x: (int(x.create_time), x.id)):
            tm = datetime.fromtimestamp(int(m.create_time) / 1000).strftime("%m-%d %H:%M")
            print(f"  挪入: id={m.id} {tm} {(m.text or '')[:40]!r}")
        if args.dry_run:
            print("dry-run, 未改库")
            return 0

        for m in moved:
            m.root_message_id = tgt.root_message_id
            m.conversation_id = tgt.id
            if args.topic:
                m.topic = args.topic
        # 随动的回复边(child 被挪走的)移到目标对话
        moved_ids = {m.message_id for m in moved}
        links = (
            s.query(MessageLink)
            .filter(MessageLink.conversation_id == src.id,
                    MessageLink.child_message_id.in_(moved_ids))
            .all()
        )
        for l in links:
            l.conversation_id = tgt.id
        # 目标对话补 topic(若为空)
        if args.topic and not tgt.topic:
            tgt.topic = args.topic
        convo = ConversationRepo(s)
        convo.update_group_stats(tgt.id)
        convo.update_group_stats(src.id)
        s.commit()
        print(f"完成: 挪入 {len(moved)} 条, 移动 {len(links)} 条回复边")
        print(f"结果: #{src.id} msgs={src.message_count}, #{tgt.id} msgs={tgt.message_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
