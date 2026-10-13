#!/usr/bin/env python3
"""回填脚本:为已入库但缺少 image_url 的图片消息补下载上传。

背景:图片消息入库时把 image_key 下载上传七牛生成 image_url。
早期版本/下载失败(如缺 message_id 导致 234001)、或部署前已入库的图片,
其 raw.image_url 为空。本脚本扫描这些消息,重新调 ImageService 补拉,幂等可重复运行。

用法:
    python scripts/backfill_images.py [--chat oc_xxx] [--limit 500] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.image_service import get_image_service  # noqa: E402

logger = logging.getLogger("backfill_images")


def _pending(session, chat_id: str | None, limit: int) -> list:
    """取 image 类型且 image_url 为空(缺落盘)的消息。"""
    stmt = (
        select(Message)
        .where(Message.message_type == "image")
        .order_by(Message.id)
        .limit(limit)
    )
    if chat_id:
        stmt = stmt.where(Message.chat_id == chat_id)

    out = []
    for m in session.execute(stmt).scalars().all():
        raw = m.raw if isinstance(m.raw, dict) else {}
        if not raw.get("image_url"):
            out.append(m)
    return out


def backfill(dry_run: bool, chat_id: str | None, limit: int) -> int:
    """回填图片 image_url,返回成功更新的条数。"""
    svc = get_image_service()
    with SessionLocal() as session:
        pending = _pending(session, chat_id, limit)
        if not pending:
            logger.info("没有待回填的图片消息(或全部已落盘)")
            return 0

        logger.info("待回填图片消息 %d 条, dry_run=%s", len(pending), dry_run)
        ok = 0
        for m in pending:
            raw = m.raw if isinstance(m.raw, dict) else {}
            image_key = raw.get("image_key") or ""
            if not image_key:
                logger.warning("跳过无 image_key 的图片消息 id=%s", m.id)
                continue
            url = svc.resolve_image_url(image_key, m.message_id or "")
            if not url:
                logger.warning("回填失败,保留占位 image_key=%s", image_key)
                continue
            raw["image_url"] = url
            m.raw = raw
            if not dry_run:
                session.add(m)
            ok += 1
            logger.info("[%d/%d] %s -> %s", ok, len(pending), image_key, url)

        if not dry_run:
            session.commit()
            logger.info("已更新 %d 条图片消息", ok)
        else:
            logger.info("[dry-run] 将更新 %d 条图片消息", ok)
        return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="回填图片消息的 image_url(下载上传七牛)")
    ap.add_argument("--chat", default=None, help="仅回填指定 chat_id")
    ap.add_argument("--limit", type=int, default=500, help="最多扫描条数(默认500)")
    ap.add_argument("--dry-run", action="store_true", help="只预览,不改库")
    args = ap.parse_args()

    log_file = "data/backfill_images.log" if not args.dry_run else ""
    setup_logging(level="INFO", log_file=log_file)

    n = backfill(args.dry_run, args.chat, args.limit)
    print(f"回填完成: 更新 {n} 条图片消息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
