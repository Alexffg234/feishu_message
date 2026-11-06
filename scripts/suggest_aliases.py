#!/usr/bin/env python3
"""别名表冷启动:把库里已有的「非标准标签」收敛到标准标签(建议 → 确认 → 落表)。

为什么需要它
------------
库里 62 个标签里只有 41 个在标准集内,其余是历史自由标签(含同义分裂:
画质模糊族 4 个、语音族 6 个)。闭集判别之后 LLM 不会再产生新自由标签,
但历史标签还在,`--reclassify` 也会重新撞上它们。

`topic_aliases`(alias -> canonical)是唯一能让它们**可审计、可回滚**地收敛
到标准标签的通道,而它现在是 0 行。本脚本不猜:对每个非标准标签用 2-gram
Dice 找最相近的标准标签并打印分数,由人判断;`--apply` 才写入别名表。

用法:
    python scripts/suggest_aliases.py                          # 全部标签 vs 标准集
    python scripts/suggest_aliases.py --min-score 0.5          # 只看较可信的
    python scripts/suggest_aliases.py --apply --min-score 0.6  # 达标的写入别名表
    python scripts/suggest_aliases.py --unmatched              # 只列找不到候选的

注意:别名表只影响「以后」的分类收敛;**历史消息的 topic 不会被本脚本改写**。
要改写存量标签需另跑 label_merge(近义归并,会物理合并同标签组),
建议在 `scripts/split_cases.py` 切分之后再做,避免把不同问题重新并到一起。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.models.topic_alias import TopicAlias  # noqa: E402
from app.services.label_norm import _label_dice  # noqa: E402
from app.services.topic_service import get_standard_labels  # noqa: E402


def _label_counts(session) -> list[tuple[str, int]]:
    """[(topic, 组数)],按组数降序。"""
    stmt = (
        select(
            Message.topic,
            func.count(func.distinct(Message.root_message_id)).label("n"),
        )
        .where(Message.topic != "", Message.root_message_id != "")
        .group_by(Message.topic)
        .order_by(func.count(func.distinct(Message.root_message_id)).desc())
    )
    return [(r[0], int(r[1] or 0)) for r in session.execute(stmt).all()]


def _best_standard(label: str, standards: list[str]) -> tuple[str, float]:
    """返回最相近的标准标签及 Dice 分数。"""
    best, score = "", 0.0
    for cand in standards:
        if cand == label:
            continue
        s = _label_dice(label, cand)
        if s > score:
            best, score = cand, s
    return best, score


def main() -> int:
    parser = argparse.ArgumentParser(description="非标准标签 -> 标准标签的别名建议")
    parser.add_argument("--min-score", type=float, default=0.0, help="只显示分数不低于此值的行")
    parser.add_argument("--apply", action="store_true", help="把达标的建议写入别名表")
    parser.add_argument("--unmatched", action="store_true", help="只列找不到合格候选的标签")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        standards = get_standard_labels(session)
        if not standards:
            print("未配置标准标签(topic_groups 为空),无法建议映射。")
            return 1
        std_set = set(standards)
        labels = _label_counts(session)
        existing_aliases = {
            a for (a,) in session.execute(select(TopicAlias.alias)).all()
        }

        free = [(t, n) for t, n in labels if t not in std_set]
        print(
            f"标签 {len(labels)} 个:标准内 {len(labels) - len(free)} 个,"
            f"非标准 {len(free)} 个;标准集共 {len(standards)} 个标签,"
            f"已有别名 {len(existing_aliases)} 条\n"
        )
        if not free:
            print("没有非标准标签,别名表无需冷启动。")
            return 0

        rows = []
        for label, n in free:
            best, score = _best_standard(label, standards)
            rows.append((label, n, best, score, label in existing_aliases))

        shown = rows
        if args.unmatched:
            shown = [r for r in rows if r[3] < max(args.min_score, 1e-9)]
        elif args.min_score > 0:
            shown = [r for r in rows if r[3] >= args.min_score]

        header = f"{'组数':>4}  {'分数':>5}  {'非标准标签':<22} -> 最相近标准标签"
        print(header)
        print("-" * 72)
        for label, n, best, score, has_alias in shown:
            flag = " [已映射]" if has_alias else ""
            print(f"{n:>4}  {score:>5.2f}  {label:<22} -> {best or '(无候选)'}{flag}")

        if not args.apply:
            print(
                f"\n以上仅为建议,未写库。确认无误后加 --apply --min-score {args.min_score or 0.6} 落表。"
            )
            return 0

        # 只写「达到阈值 + 还没映射过」的,auto=1 标记为待人工复核
        threshold = args.min_score if args.min_score > 0 else 0.6
        added = skipped = 0
        for label, _n, best, score, has_alias in rows:
            if has_alias or not best or score < threshold:
                skipped += 1
                continue
            session.add(TopicAlias(alias=label, canonical=best, auto=1))
            added += 1
        session.commit()
        print(
            f"\n已写入别名 {added} 条(阈值 {threshold}),跳过 {skipped} 条。"
            f"确认后可用 scripts/add_alias.py --list 复核。"
        )
        print(
            "提示:别名只影响之后的分类收敛;改写存量标签需另跑 label_merge,"
            "且会物理合并同标签组,建议放在切分之后。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
