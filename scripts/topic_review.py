#!/usr/bin/env python3
"""人工复核闭环:列队列 -> 定标 -> 反哺别名 -> 看 gold set 准确率。

为什么需要它
------------
闭集判别、候选召回、相似度兜底,都只能把「模型判得对的」判对。剩下判不对的
必须有人看一遍,而且**看一遍的价值要沉淀下来**,否则同一类错会一直错:

    人工改对一组 -> 回写库(置 source=human) -> 写进 topic_aliases
                 -> 该组进 gold set -> 用它评准确率 -> 再调提示词/召回

没有 gold set,所有「准确率」都只是模型自说自话。这是唯一能回答「这次改动
到底有没有更准」的东西。

用法:
    # 1. 看队列(未分类 + 低置信,未分类优先),带样本文本
    python scripts/topic_review.py
    python scripts/topic_review.py --chat oc_xxx --limit 200 --low-conf 0.6

    # 2. 导出 csv(填好 label 列再倒回来,适合批量)
    python scripts/topic_review.py --export review.csv
    python scripts/topic_review.py --csv review.csv --apply

    # 3. 单条定标
    python scripts/topic_review.py --group <root_message_id> --label 屏幕闪烁 \
        --chat oc_xxx --apply --propagate

    # 4. gold set 规模 / 准确率(准确率需配置 LLM)
    python scripts/topic_review.py --gold
    python scripts/topic_review.py --gold --eval
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.services.topic_review import (  # noqa: E402
    DEFAULT_LOW_CONF,
    apply_review,
    eval_gold,
    gold_stats,
    list_review_queue,
)


def _fmt_ms(create_time: str) -> str:
    """毫秒时间戳字符串 -> 'YYYY-MM-DD HH:MM';不可解析原样返回。"""
    try:
        return datetime.datetime.fromtimestamp(int(create_time) / 1000).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError):
        return create_time or "-"


def _print_queue(session, args) -> int:
    q = list_review_queue(
        session, chat_id=args.chat or None, limit=args.limit, low_conf=args.low_conf
    )
    if not q["total"]:
        print("复核队列为空:没有未分类组,也没有低置信组。")
        return 0
    print(
        f"待复核 {q['total']} 组(低置信线 {args.low_conf});"
        f"下面列出 {len(q['items'])} 组(--limit 可调)"
    )
    print(f"\n{'时间':<17} {'条数':>4}  {'组根':<14} {'原因':<13} {'现值':<12} 样本")
    print("-" * 100)
    for it in q["items"]:
        reason = "未分类" if it["reason"] == "unclassified" else f"低置信{it['topic_conf']:.2f}"
        topic = it["topic"] or "(空)"
        sample = " / ".join(it["samples"])[:46].replace("\n", " ")
        print(
            f"{_fmt_ms(it['last_time']):<17} {it['message_count']:>4}  "
            f"{it['root_message_id'][:12]:<14} {reason:<13} {topic[:10]:<12} {sample}"
        )
    return 0


def _export_csv(session, args) -> int:
    q = list_review_queue(
        session, chat_id=args.chat or None, limit=args.limit, low_conf=args.low_conf
    )
    path = Path(args.export)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            ["chat_id", "root_message_id", "reason", "current_topic", "confidence",
             "message_count", "label", "sample"]
        )
        for it in q["items"]:
            w.writerow(
                [
                    it["chat_id"],
                    it["root_message_id"],
                    it["reason"],
                    it["topic"],
                    round(it["topic_conf"], 3),
                    it["message_count"],
                    "",  # 待人工填写
                    " / ".join(it["samples"])[:200],
                ]
            )
    print(f"已导出 {len(q['items'])} 组到 {path}")
    print("填好 label 列(必须是标准标签)后执行:"
          f" python scripts/topic_review.py --csv {path} --apply")
    return 0


def _apply_csv(session, args) -> int:
    path = Path(args.csv)
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1
    ok = skipped = failed = 0
    head = "预览" if not args.apply else "写库"
    print(f"[{head}] 按 {path} 批量定标")
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            label = (row.get("label") or "").strip()
            if not label:
                skipped += 1
                continue
            r = apply_review(
                session,
                row["chat_id"],
                row["root_message_id"],
                label,
                allow_new=args.allow_new,
                propagate=args.propagate,
                dry_run=not args.apply,
            )
            if r["ok"]:
                ok += 1
                alias = r["aliases_written"][0] if r["aliases_written"] else r["alias_skipped"]
                print(
                    f"  ok  {row['root_message_id'][:12]} "
                    f"{r['old_topic'] or '(空)'} -> {r['label']}  [{alias}]"
                )
            else:
                failed += 1
                print(f"  !!  {row['root_message_id'][:12]} {r['reason']}")
    print(f"\n成功 {ok} / 跳过(未填) {skipped} / 失败 {failed}")
    if not args.apply:
        print("以上为预览,未修改任何数据;确认无误后加 --apply 执行")
    return 0 if not failed else 2


def _apply_one(session, args) -> int:
    r = apply_review(
        session,
        args.chat,
        args.group,
        args.label,
        allow_new=args.allow_new,
        propagate=args.propagate,
        dry_run=not args.apply,
    )
    if not r["ok"]:
        print(f"失败: {r['reason']}")
        if r.get("standards_hint"):
            print(f"标准标签示例: {'、'.join(r['standards_hint'])}")
        return 2
    prefix = "[预览]" if r["dry_run"] else "已定标"
    print(f"{prefix} {args.group} : {r['old_topic'] or '(空)'} -> {r['label']}")
    print(f"  影响消息 {r['messages']} 条;topic_source=human,置信度 1.0")
    if r["aliases_written"]:
        print(f"  已学别名: {', '.join(r['aliases_written'])}(以后同类输出自动收敛)")
    elif r["alias_skipped"]:
        print(f"  未写别名: {r['alias_skipped']}")
    if r["propagated_messages"]:
        print(f"  连带回刷 {r['propagated_messages']} 条同标签消息")
    if r["dry_run"]:
        print("以上为预览,未修改任何数据;确认无误后加 --apply 执行")
    return 0


def _gold(session, args) -> int:
    stats = gold_stats(session, chat_id=args.chat or None)
    if not stats["gold_groups"]:
        print(
            "gold set 为空:还没有人工确认过的组。\n"
            "先跑 python scripts/topic_review.py 看队列,再用 --apply 定标几组——\n"
            "没有 gold set,任何「准确率」都只是模型自说自话。"
        )
        return 0
    print(
        f"gold set: {stats['gold_groups']} 组 / 全部 {stats['total_groups']} 组"
        f"(人工覆盖率 {stats['human_rate']:.1%})"
    )
    print("标签分布:")
    for label, n in stats["labels"].items():
        print(f"  {n:>4}  {label}")
    if not args.eval:
        print("\n加 --eval 用当前判别器重跑这些组,算 top-1 准确率(需配置 LLM)")
        return 0

    print("\n正在重跑判别(与线上同一条路径:召回 -> 判别 -> 归一化)...")
    res = eval_gold(session, chat_id=args.chat or None, limit=args.limit)
    if not res["ok"]:
        print(f"无法评估: {res['reason']}")
        return 1
    print(
        f"准确率 {res['accuracy']:.1%}(命中 {res['hit']} / 判出 {res['hit'] + res['miss']}),"
        f"未归类 {res['unknown']},覆盖率 {res['coverage']:.1%}"
    )
    if res["misses"]:
        print("\n判错的组(人工 gold vs 模型预测):")
        for m in res["misses"]:
            print(
                f"  {m['root_message_id'][:12]:<14} gold={m['gold']:<14} "
                f"pred={m['pred']:<14} conf={m['conf']}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="人工复核闭环(队列 / 定标 / gold set)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--chat", default="", help="chat_id(--group 定标时必填)")
    parser.add_argument("--limit", type=int, default=50, help="最多列出/处理 N 组")
    parser.add_argument(
        "--low-conf", type=float, default=DEFAULT_LOW_CONF, help="低置信线,低于它入队"
    )
    parser.add_argument("--export", default="", help="导出待复核 csv(填 label 列)")
    parser.add_argument("--csv", default="", help="按 csv 批量定标")
    parser.add_argument("--group", default="", help="单组定标:root_message_id")
    parser.add_argument("--label", default="", help="单组定标:标准标签")
    parser.add_argument("--apply", action="store_true", help="真正写库(否则只看)")
    parser.add_argument(
        "--allow-new", action="store_true", help="允许写闭集外的标签(慎用)"
    )
    parser.add_argument(
        "--propagate", action="store_true", help="把库里同错标签的组一并回刷"
    )
    parser.add_argument("--gold", action="store_true", help="看 gold set 规模/准确率")
    parser.add_argument("--eval", action="store_true", help="配合 --gold:重跑判别算准确率")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        if args.gold:
            return _gold(session, args)
        if args.csv:
            return _apply_csv(session, args)
        if args.group:
            if not args.chat:
                print("--group 定标必须同时给 --chat")
                return 1
            if not args.label:
                print("--group 定标必须同时给 --label")
                return 1
            return _apply_one(session, args)
        if args.export:
            return _export_csv(session, args)
        return _print_queue(session, args)


if __name__ == "__main__":
    raise SystemExit(main())
