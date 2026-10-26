#!/usr/bin/env python3
"""回填脚本:给存量对话标记「新问题块起点」(messages.block_start=1)。

背景:block_start 由 merge_groups 在「带已有标签的新消息并入既有对话」时
自动标记,供 /summary 完整对话页画横杠。存量数据在引入该逻辑前就已归并,
无此标记,本脚本一次性补齐。

默认目标:topic 含「住居美学馆」的对话(按用户要求,存量只处理该对话)。
规则:取对话内按时间排序的相邻消息中「最大时间断档」,断档后的首条消息
即为新问题起点(该对话两个问题相隔 11 天,远大于问题内部任何间隔)。
幂等:目标消息已标记时直接跳过,可重复执行。

用法:
    python scripts/backfill_block_start.py --dry-run            # 预览不写库
    python scripts/backfill_block_start.py                       # 执行默认目标
    python scripts/backfill_block_start.py --conversation-id 125 # 指定对话
    python scripts/backfill_block_start.py --message-id om_xxx   # 直接指定起点消息
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.message import Message  # noqa: E402


def _to_ms(create_time: str) -> int:
    """毫秒时间戳字符串 -> int;非数字返回 0。"""
    t = (create_time or "").strip()
    return int(t) if t.isdigit() else 0


def _list_conversation_messages(session, conversation_id: int) -> list[Message]:
    rows = (
        session.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .all()
    )
    rows.sort(key=lambda m: (_to_ms(m.create_time), m.id))
    return rows


def pick_target(session, conversation_id: int) -> Message | None:
    """取「最大时间断档」后的首条消息;消息不足 2 条返回 None。"""
    rows = _list_conversation_messages(session, conversation_id)
    if len(rows) < 2:
        return None
    best_gap = -1
    target: Message | None = None
    for prev, cur in zip(rows, rows[1:]):
        gap = _to_ms(cur.create_time) - _to_ms(prev.create_time)
        if gap > best_gap:
            best_gap = gap
            target = cur
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只预览,不写库")
    parser.add_argument("--conversation-id", type=int, default=0, help="指定对话 id")
    parser.add_argument("--message-id", default="", help="直接指定起点消息 message_id")
    parser.add_argument("--topic-like", default="%住居美学馆%", help="按 topic 模糊匹配定位对话")
    args = parser.parse_args()

    setup_logging()
    with SessionLocal() as session:
        target_msg: Message | None = None

        if args.message_id:
            target_msg = (
                session.query(Message).filter(Message.message_id == args.message_id).first()
            )
            if target_msg is None:
                print(f"未找到 message_id={args.message_id}")
                return
            conv_id = target_msg.conversation_id
            print(f"指定起点消息: {args.message_id} (对话 #{conv_id})")
        else:
            if args.conversation_id:
                conv = session.get(Conversation, args.conversation_id)
            else:
                conv = (
                    session.query(Conversation)
                    .filter(Conversation.topic.like(args.topic_like))
                    .order_by(Conversation.id.asc())
                    .first()
                )
            if conv is None:
                print(f"未找到目标对话(conversation_id={args.conversation_id or '-'}, topic like {args.topic_like})")
                return
            conv_id = conv.id
            print(f"目标对话: #{conv.id} topic={conv.topic!r}")
            target_msg = pick_target(session, conv_id)
            if target_msg is None:
                print("对话内消息不足 2 条,无需标记")
                return
            print(
                f"最大时间断档后的首条消息: {target_msg.message_id} "
                f"(create_time={target_msg.create_time}, 当前 block_start={target_msg.block_start})"
            )
            if int(target_msg.block_start or 0) == 1:
                print("该消息已标记 block_start=1,幂等跳过")
                return

        if args.dry_run:
            print("[dry-run] 不写库")
            return
        target_msg.block_start = 1
        session.commit()
        print(f"已标记 block_start=1: {target_msg.message_id}")


if __name__ == "__main__":
    main()
