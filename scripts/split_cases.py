#!/usr/bin/env python3
"""问题单元切分:把混了多个问题的对话切成「一个问题一个对话」。

背景见 app/services/case_split.py。本脚本是命令行入口:
默认只出报告(dry-run),--apply 才落库;切完先让新单元「未分类」,
再加 --relabel 让它们立刻各自重新归类(需要 LLM 配置)。

用法:
    # 1. 先看阈值怎么选(不写库):各阈值会切出多少单元
    python scripts/split_cases.py --stats

    # 2. 预览某个阈值下的切分效果(不写库)
    python scripts/split_cases.py --gap-hours 2 --limit 10

    # 3. 落库(新单元标签清空,等后续归类)
    python scripts/split_cases.py --gap-hours 2 --apply

    # 4. 落库并立即给新单元重新打标签(需 LLM_API_KEY/LLM_MODEL)
    python scripts/split_cases.py --gap-hours 2 --apply --relabel

    # 只处理某个群 / 某个对话
    python scripts/split_cases.py --chat oc_xxx --apply
    python scripts/split_cases.py --conversation-id 125 --apply

幂等:切过之后组内相邻消息不会再超过阈值(切点已无),重复执行只在
「切完又灌入跨小时距新消息」时才产生新切分。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.case_split import (  # noqa: E402
    _DEFAULT_GAP_SEC,
    plan_pieces,
    split_conversation_by_case,
)

# --stats 要对比的阈值(小时)
_STAT_THRESHOLDS = (0.5, 1.0, 2.0, 4.0, 8.0, 24.0)


def _load_conversations(session, chat: str = "", conv_id: int = 0):
    """返回 [(Conversation, [Message...])],按对话 id 升序。"""
    stmt = select(Conversation)
    if chat:
        stmt = stmt.where(Conversation.chat_id == chat)
    if conv_id:
        stmt = stmt.where(Conversation.id == conv_id)
    convs = list(session.scalars(stmt.order_by(Conversation.id)).all())
    if not convs:
        return []
    ids = [c.id for c in convs]
    by_conv: dict[int, list[Message]] = defaultdict(list)
    for m in session.scalars(
        select(Message).where(Message.conversation_id.in_(ids))
    ).all():
        by_conv[m.conversation_id].append(m)
    return [(c, by_conv.get(c.id, [])) for c in convs]


def _print_stats(convs) -> None:
    """各阈值下的切分规模对比,用于挑 --gap-hours。"""
    total = len(convs)
    total_msgs = sum(len(rows) for _, rows in convs)
    print(f"对话 {total} 个 / 消息 {total_msgs} 条\n")
    header = f"{'阈值':>6} {'可切对话':>8} {'切出单元':>8} {'单元均值':>8} {'最大单元':>8}"
    print(header)
    print("-" * len(header))
    for hours in _STAT_THRESHOLDS:
        gap_sec = int(hours * 3600)
        splittable = 0
        units = 0
        biggest = 0
        for _, rows in convs:
            pieces = plan_pieces(rows, gap_sec)
            units += len(pieces)
            biggest = max(biggest, max((len(p["rows"]) for p in pieces), default=0))
            if len(pieces) > 1:
                splittable += 1
        avg = (total_msgs / units) if units else 0.0
        print(f"{hours:>5.1f}h {splittable:>8} {units:>8} {avg:>8.1f} {biggest:>8}")
    print(
        "\n说明:单元均值越小切得越碎。切碎只是多几个单元,切不开则"
        "一个标签要覆盖多个问题——宁碎勿混。"
    )


def _relabel_run(queue: list[tuple[str, str]]) -> tuple[int, int]:
    """给新单元重新归类;返回 (成功数, 失败数)。未配置 LLM 时直接跳过。"""
    settings = get_settings()
    if not settings.llm_enabled:
        print("未配置 LLM(LLM_API_KEY/LLM_MODEL),跳过 --relabel")
        return 0, 0
    from app.services.llm import LLMClient
    from app.services.topic_service import TopicService

    llm = LLMClient(
        settings.llm_api_base,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_timeout,
    )
    svc = TopicService(llm=llm)
    ok = fail = 0
    for i, (chat_id, root) in enumerate(queue, start=1):
        try:
            label = svc.classify_group(chat_id, root, merge=False)
            ok += 1
            print(f"  [{i}/{len(queue)}] {root} -> {label or '(未分类)'}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  [{i}/{len(queue)}] {root} 归类失败: {e}")
    return ok, fail


def main() -> int:
    parser = argparse.ArgumentParser(description="按问题单元切分对话")
    parser.add_argument("--chat", default="", help="只处理指定 chat_id")
    parser.add_argument("--conversation-id", type=int, default=0, help="只处理指定对话")
    parser.add_argument(
        "--gap-hours",
        type=float,
        default=_DEFAULT_GAP_SEC / 3600,
        help="时间断档阈值(小时),默认 2",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个可切对话(0=全部)")
    parser.add_argument("--apply", action="store_true", help="落库(默认只出报告)")
    parser.add_argument("--relabel", action="store_true", help="切完立即给新单元重新归类(需 LLM)")
    parser.add_argument("--stats", action="store_true", help="只打印各阈值的切分规模对比")
    args = parser.parse_args()

    init_db()
    gap_sec = int(args.gap_hours * 3600)

    with SessionLocal() as session:
        convs = _load_conversations(session, args.chat, args.conversation_id)
        if not convs:
            print("没有匹配的对话")
            return 0
        if args.stats:
            _print_stats(convs)
            return 0

    # 只挑真正能切的对话,再按 --limit 截断
    todo: list[tuple[int, str, int]] = []
    for conv, rows in convs:
        pieces = plan_pieces(rows, gap_sec)
        if len(pieces) > 1:
            todo.append((conv.id, conv.chat_id, len(pieces)))
    print(
        f"阈值 {args.gap_hours:g}h:可切对话 {len(todo)} / {len(convs)},"
        f"这些对话将切成 {sum(p for _, _, p in todo)} 个单元"
    )
    if args.limit:
        todo = todo[: args.limit]
        print(f"按 --limit 只处理前 {len(todo)} 个")
    if not todo:
        return 0

    reasons: Counter[str] = Counter()
    relabel_queue: list[tuple[str, str]] = []
    done = failed = 0
    for i, (cid, chat_id, _) in enumerate(todo, start=1):
        with SessionLocal() as session:
            try:
                report = split_conversation_by_case(
                    session, chat_id, cid, gap_sec=gap_sec, apply=args.apply
                )
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"[{i}/{len(todo)}] #{cid} 失败: {e}")
                continue
        if not report.get("split"):
            continue
        done += 1
        reasons.update(report.get("cut_reasons") or [])
        detail = " ".join(
            f"{g['root'][:8]}:{g['count']}" for g in report["groups"]
        )
        print(
            f"[{i}/{len(todo)}] #{cid} {report['messages']} 条 -> "
            f"{report['pieces']} 个单元 ({detail})"
        )
        if args.apply:
            for g in report["groups"]:
                relabel_queue.append((chat_id, g["root"]))

    print(
        f"\n{'已切分' if args.apply else '可切分(未落库)'} {done} 个对话,"
        f"失败 {failed};切点来源 {dict(reasons)}"
    )
    if args.apply and args.relabel and relabel_queue:
        print(f"\n重新归类 {len(relabel_queue)} 个单元:")
        ok, fail = _relabel_run(relabel_queue)
        print(f"归类完成:成功 {ok},失败 {fail}")
    elif args.apply:
        print(
            "新单元标签已清空(未分类)。后续:重跑分类(--relabel)或让新消息触发的"
            "自动归类补齐。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
