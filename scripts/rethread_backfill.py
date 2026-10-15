#!/usr/bin/env python3
"""回填脚本:按 LLM 主题标签把同群同主题的回复链组归并为一个对话。

背景:显式回复靠回复链归组、同人连发靠轮次合并归组;「直接发言无回复」的
碎片反馈(如连串追问)在库里各自成组。本脚本对存量数据:
  1) (可选 --relabel) 清空存量标签,用新提示词(带已有主题样本)重打全部组
  2) (可选 --reclassify) 给未打标签的组补调 LLM 分类
  3) (可选 --consolidate) LLM 判定同群内近义标签(如 亮度调节/本控按键)
     并统一到保留标签
  4) 按 (chat_id, topic) 规划归并对,把较新的组并入最早组(原问题块保留为根)

幂等可重复运行;--dry-run 只预览最后一步的归并对(前面步骤仍会写标签)。

用法:
    python scripts/rethread_backfill.py --dry-run          # 预览将归并的组对
    python scripts/rethread_backfill.py                    # 执行归并
    python scripts/rethread_backfill.py --reclassify       # 先补打标签再归并
    python scripts/rethread_backfill.py --relabel --consolidate  # 重打标签+近义标签归并
    python scripts/rethread_backfill.py --chat oc_xxx      # 仅处理指定群
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import update  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.dao.message_repo import MessageRepo  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.group_merge import merge_groups  # noqa: E402

logger = logging.getLogger("rethread_backfill")


def _clear_labels(session, chat_id: str | None) -> int:
    """清空(范围)内全部标签,返回受影响的消息条数。供 --relabel 重打前使用。"""
    stmt_m = update(Message).values(topic="")
    stmt_c = update(Conversation).values(topic="")
    if chat_id:
        stmt_m = stmt_m.where(Message.chat_id == chat_id)
        stmt_c = stmt_c.where(Conversation.chat_id == chat_id)
    n = session.execute(stmt_m).rowcount or 0
    session.execute(stmt_c)
    session.commit()
    return n


def _consolidate(chat_id: str | None) -> int:
    """LLM 判定同群近义标签并统一,随后触发同标签归并。"""
    from app.core.config import get_settings
    from app.services.label_merge import consolidate_labels
    from app.services.llm import LLMClient

    settings = get_settings()
    if not settings.llm_enabled:
        logger.error("LLM 未配置(需设置 LLM_API_KEY / LLM_MODEL),无法标签归并")
        return 1
    llm = LLMClient(
        settings.llm_api_base,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_timeout,
    )
    with SessionLocal() as session:
        applied = consolidate_labels(session, llm, chat_id)
    for old, new in applied:
        print(f"  标签 {old} -> {new}")
    print(f"标签归并完成: 应用 {len(applied)} 条映射")
    return 0


def plan(session, chat_id: str | None) -> list[tuple[str, str, str, str]]:
    """规划全部归并对,返回 [(chat_id, topic, source_root, target_root)]。

    按 (chat_id, topic) 分桶,桶内最早组(first_message_id 最小)为目标。
    只读不改库,供 dry-run 预览。
    """
    buckets: dict[tuple[str, str], list[str]] = {}
    repo = MessageRepo(session)
    for c, topic, root, _fid in repo.list_all_topic_groups(chat_id=chat_id):
        buckets.setdefault((c, topic), []).append(root)

    pairs: list[tuple[str, str, str, str]] = []
    for (c, topic), roots in buckets.items():
        if len(roots) < 2:
            continue
        target = roots[0]  # 列表已按最早消息升序
        for source in roots[1:]:
            pairs.append((c, topic, source, target))
    return pairs


def _reclassify(chat_id: str | None) -> tuple[int, int, int]:
    """给未打标签的组补调 LLM 分类(只打标签不归并)。返回 (新分类, 已有, 失败)。"""
    from app.services.topic_service import get_topic_service

    service = get_topic_service()
    if not service.enabled:
        logger.error("LLM 未配置(需设置 LLM_API_KEY / LLM_MODEL),无法补分类")
        return 0, 0, -1

    with SessionLocal() as session:
        roots = MessageRepo(session).list_roots(chat_id=chat_id, limit=100000)
        pending = [
            (r, c)
            for r, c in roots
            if not MessageRepo(session).group_has_topic(c, r)
        ]
    # list_roots 按最新在前;改为从最早组开始,使后续组的前文已带标签可作上下文
    pending.reverse()
    logger.info("待补分类 %d 组(共 %d 组)", len(pending), len(roots))

    ok = fail = 0
    for i, (root_id, c) in enumerate(pending, 1):
        try:
            service.classify_group(c, root_id, merge=False)
            ok += 1
            logger.info("[%d/%d] %s/%s 已分类", i, len(pending), c, root_id)
        except Exception as e:  # noqa: BLE001
            fail += 1
            logger.warning("[%d/%d] %s/%s 分类失败: %s", i, len(pending), c, root_id, e)
    return ok, len(pending) - ok - fail, fail


def backfill(dry_run: bool, chat_id: str | None) -> int:
    """执行(或预览)同主题归并,返回实际/将归并的组对数。"""
    with SessionLocal() as session:
        pairs = plan(session, chat_id)

    if not pairs:
        logger.info("没有可归并的同主题组对(已全部归并,或未打标签)")
        return 0

    logger.info(
        "%s同主题组对 %d 组对, dry_run=%s",
        "[dry-run] 将归并 " if dry_run else "归并 ",
        len(pairs),
        dry_run,
    )
    if dry_run:
        for c, topic, source, target in pairs:
            print(f"  {c} [{topic}]: {source} -> {target}")
        return len(pairs)

    done = fail = 0
    with SessionLocal() as session:
        for c, topic, source, target in pairs:
            try:
                if merge_groups(session, c, source, target, topic):
                    done += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                logger.exception("归并失败 %s: %s -> %s (%s)", c, source, target, e)
    logger.info("归并完成: 成功 %d 组对, 失败 %d", done, fail)
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="按主题标签归并同群回复链组")
    ap.add_argument("--chat", default=None, help="仅处理指定 chat_id")
    ap.add_argument("--dry-run", action="store_true", help="只预览最后一步的归并对,不改库")
    ap.add_argument(
        "--reclassify", action="store_true", help="先给未打标签的组补调 LLM 分类"
    )
    ap.add_argument(
        "--relabel",
        action="store_true",
        help="清空(范围内)全部标签后用新提示词重打(碎片标签收敛)",
    )
    ap.add_argument(
        "--consolidate",
        action="store_true",
        help="LLM 判定同群近义标签并统一到保留标签,再触发同标签归并",
    )
    args = ap.parse_args()

    log_file = "data/rethread_backfill.log" if not args.dry_run else ""
    setup_logging(level="INFO", log_file=log_file)

    if args.relabel:
        with SessionLocal() as session:
            n = _clear_labels(session, args.chat)
        print(f"已清空 {n} 条消息的 topic(--relabel)")

    if args.reclassify or args.relabel:
        ok, skip, fail = _reclassify(args.chat)
        if fail < 0:
            return 1
        print(f"分类完成: 新分类 {ok}, 已有标签 {skip}, 失败 {fail}")

    if args.consolidate:
        rc = _consolidate(args.chat)
        if rc != 0:
            return rc

    n = backfill(args.dry_run, args.chat)
    verb = "将归并" if args.dry_run else "已归并"
    print(f"同主题归组完成: {verb} {n} 组对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
