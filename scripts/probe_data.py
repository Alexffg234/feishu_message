#!/usr/bin/env python3
"""数据探查:分析群聊消息的规模、时间分布、类型分布与当前分组状态。

用途:为「话题段落重新归类」设计提供真实数据依据(时序参数、消息量级估算)。
用法(服务器上,务必用项目 venv):
    ./.venv/bin/python scripts/probe_data.py [--chat oc_xxx] [--limit 2000]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.models.conversation import Conversation, MessageLink  # noqa: E402


def _fmt(ts_str: str) -> str:
    import datetime
    try:
        s = int(ts_str)
        if s > 10**12:  # 毫秒
            s /= 1000
        return datetime.datetime.fromtimestamp(s).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ts_str or "-"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default=None, help="只探查某群 chat_id")
    ap.add_argument("--limit", type=int, default=2000, help="最多取多少条消息分析")
    args = ap.parse_args()

    with SessionLocal() as s:
        # ---- 总量 ----
        total = s.execute(select(func.count(Message.id))).scalar_one()
        print(f"=== 消息总量: {total} ===")

        # ---- 群里分组 ----
        chats = s.execute(
            select(Message.chat_id, func.count(Message.id))
            .group_by(Message.chat_id)
            .order_by(func.count(Message.id).desc())
            .limit(10)
        ).all()
        print("\n=== 各群消息数 TOP10 ===")
        for cid, cnt in chats:
            print(f"  {cid}: {cnt} 条")

        # ---- 目标群取样本 ----
        q = select(Message)
        if args.chat:
            q = q.where(Message.chat_id == args.chat)
        q = q.order_by(Message.received_at.asc(), Message.id.asc()).limit(args.limit)
        rows = list(s.scalars(q).all())
        print(f"\n=== 抽样消息 {len(rows)} 条(时间正序) ===")

        if rows:
            t0 = rows[0].create_time
            t1 = rows[-1].create_time
            print(f"  最早: {_fmt(t0)}  最晚: {_fmt(t1)}")
            try:
                span_min = (int(t1) - int(t0)) / 1000 / 60
                print(f"  跨度: {span_min:.0f} 分钟 (约 {span_min/60:.1f} 小时)")
            except (TypeError, ValueError):
                print("  跨度: 无法计算")

        # ---- 类型分布 ----
        from collections import Counter
        types = Counter(m.message_type for m in rows)
        print("\n=== 消息类型分布 ===")
        for t, c in types.most_common():
            print(f"  {t}: {c}")

        # ---- 发言者分布 ----
        senders = Counter(m.sender_id for m in rows if m.sender_id)
        print(f"\n=== 发言者数(样本内): {len(senders)} ===")
        for sid, c in senders.most_common(8):
            print(f"  {sid}: {c} 条")

        # ---- 消息文本长度分布(估复杂度) ----
        lens = [len((m.text or "").strip()) for m in rows if m.text.strip()]
        if lens:
            avg = sum(lens) / len(lens)
            print(f"\n=== 文本消息 {len(lens)} 条,平均长度 {avg:.0f} 字 ===")
            short = sum(1 for L in lens if L <= 20)
            print(f"  短消息(<=20字): {short} 条 ({short*100//len(lens)}%)")

        # ---- 时间间隔分布(决定段切分阈值) ----
        gaps: list[int] = []
        prev = None
        for m in rows:
            try:
                cur = int(m.create_time)
            except (TypeError, ValueError):
                cur = None
            if prev is not None and cur is not None:
                gaps.append((cur - prev) / 1000.0)  # 秒
            prev = cur
        if gaps:
            gaps.sort()
            print(f"\n=== 相邻消息间隔(秒) 分位数 (样本 {len(gaps)}) ===")
            for name, p in [("P50", 0.50), ("P80", 0.80), ("P90", 0.90), ("P95", 0.95), ("P99", 0.99)]:
                idx = int(p * (len(gaps) - 1))
                print(f"  {name}: {gaps[idx]:.0f}s")
            big = sum(1 for g in gaps if g > 900)  # >15min
            print(f"  间隔>15分钟 的断点数: {big}")

        # ---- 当前分组状态 ----
        roots = s.execute(
            select(func.count(func.distinct(Message.root_message_id)))
        ).scalar_one()
        convs = s.execute(select(func.count(Conversation.id))).scalar_one()
        links = s.execute(select(func.count(MessageLink.id))).scalar_one()
        with_topic = s.execute(
            select(func.count(Message.id)).where(Message.topic != "")
        ).scalar_one()
        print(f"\n=== 当前归类状态 ===")
        print(f"  独立 root_message_id: {roots}")
        print(f"  conversations 表记录: {convs}")
        print(f"  message_links 回复边: {links}")
        print(f"  已打 topic 标签消息: {with_topic}")

        # ---- 抽样:最近 20 条消息原文(看表达方式) ----
        print(f"\n=== 最近 20 条消息原文(看表达方式) ===")
        for m in rows[-20:]:
            txt = (m.text or "").strip().replace("\n", " ")
            print(f"  [{_fmt(m.create_time)}] {m.sender_id[:12]:12s} ({m.message_type}) {txt[:40]}")
            if m.parent_message_id:
                print(f"      ↳ 回复了 {m.parent_message_id[:12]}")

    print("\n完成。")


if __name__ == "__main__":
    main()
