#!/usr/bin/env python3
"""未分类清单:哪些回复链组没打上标签、它们到底在说什么。

为什么需要它
------------
闭集判别上线后,LLM 选不出标准标签就输出 UNKNOWN,那组会落成「未分类」。
这是设计意图(宁可空着也不写脏标签),但**未分类会静默堆积**:
`list_topics` 只查 topic != '',页面上根本看不到这批数据。没有出口,就
既不知道堆了多少,也不知道闭集缺了哪类现象。

用法:
    python scripts/unclassified.py                       # 最近 50 个未分类组
    python scripts/unclassified.py --limit 200 --chat oc_xxx
    python scripts/unclassified.py --words               # 另附高频词,看闭集缺什么
    python scripts/unclassified.py --words --top 30

判断口径:一个组(chat + root_message_id)只要有任何一条消息带非空 topic
就算已归类;整组全空才算未分类。
"""
from __future__ import annotations

import argparse
import datetime
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.dao.message_repo import MessageRepo  # noqa: E402
from app.models.message import Message  # noqa: E402
# 复用话题聚类的 2-gram 切分(label_norm 也这么用),别重复实现
from app.services.cluster_service import _char_bigrams  # noqa: E402

# 高频词统计时过滤的虚词/应答词(单字与大路货,对判断「缺哪类现象」没用)
_STOP_BIGRAMS = {
    "好的", "收到", "谢谢", "可以", "什么", "怎么", "这个", "那个", "一下",
    "我们", "你们", "现在", "还是", "就是", "但是", "已经", "问题", "麻烦",
}


def _total_groups(session, chat: str = "") -> int:
    """全部组数(chat + root 去重,用 group_by 子查询保证跨库可移植)。"""
    sub = select(Message.chat_id, Message.root_message_id).where(
        Message.root_message_id != ""
    )
    if chat:
        sub = sub.where(Message.chat_id == chat)
    sub = sub.group_by(Message.chat_id, Message.root_message_id).subquery()
    return int(session.execute(select(func.count()).select_from(sub)).scalar() or 0)


def _fmt_ms(create_time: str) -> str:
    """毫秒时间戳字符串 -> 'YYYY-MM-DD HH:MM';不可解析原样返回。"""
    try:
        return datetime.datetime.fromtimestamp(int(create_time) / 1000).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError):
        return create_time or "-"


def _first_texts(session, rows: list[dict]) -> dict[int, str]:
    """按 first_message_id 批量取组首条文本。"""
    ids = [r["first_message_id"] for r in rows if r["first_message_id"]]
    if not ids:
        return {}
    stmt = select(Message.id, Message.text, Message.message_type).where(
        Message.id.in_(ids)
    )
    return {
        int(i): (t or f"({mt or '非文本'})") for i, t, mt in session.execute(stmt).all()
    }


def _print_words(texts: list[str], top: int) -> None:
    cnt: Counter[str] = Counter()
    for t in texts:
        for bg in _char_bigrams(t):
            if bg in _STOP_BIGRAMS or len(bg) < 2:
                continue
            cnt[bg] += 1
    print(f"\n未分类组首条消息高频 2-gram(Top {top}),用来判断闭集缺哪些现象:")
    for bg, n in cnt.most_common(top):
        print(f"  {bg}  {n}")


def main() -> int:
    parser = argparse.ArgumentParser(description="列出未分类的回复链组")
    parser.add_argument("--chat", default="", help="只处理指定 chat_id")
    parser.add_argument("--limit", type=int, default=50, help="最多列出 N 组")
    parser.add_argument("--words", action="store_true", help="另附高频 2-gram")
    parser.add_argument("--top", type=int, default=20, help="高频词显示条数")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        repo = MessageRepo(session)
        rows = repo.list_unclassified_roots(args.chat, limit=args.limit)
        unclassified_total = repo.count_unclassified_roots(args.chat)
        total = _total_groups(session, args.chat)
        texts = _first_texts(session, rows)

    rate = (unclassified_total / total) if total else 0.0
    print(
        f"未分类组 {unclassified_total} / 全部 {total} 组(占比 {rate:.1%});"
        f"下面列出最近 {len(rows)} 个(--limit 可调)"
    )
    if not rows:
        print("全部已归类。")
        return 0

    print(f"\n{'时间':<17} {'条数':>4}  {'组根':<14} 首条消息")
    print("-" * 78)
    for r in rows:
        text = (texts.get(r["first_message_id"]) or "").strip().replace("\n", " ")
        ts = _fmt_ms(r["start_time"])
        print(
            f"{ts:<17} {r['message_count']:>4}  {r['root_message_id'][:12]:<14} "
            f"{text[:40]}"
        )
    if args.words:
        _print_words([texts.get(r["first_message_id"], "") for r in rows], args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
