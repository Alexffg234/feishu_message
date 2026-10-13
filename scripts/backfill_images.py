#!/usr/bin/env python3
"""回填脚本:为已入库但缺资源 URL 的图片/视频消息补下载上传。

背景:图片/视频消息入库时把资源 key 下载上传七牛生成可展示 URL。
早期版本/下载失败(如缺 message_id 导致 234001)、或部署前已入库的消息,
其 raw.image_url / raw.file_url 为空。本脚本扫描这些消息,重新拉取补货,幂等可重复运行。

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
from app.services import message_service as ms  # noqa: E402

logger = logging.getLogger("backfill_images")

# 需要在资源消息上补齐的字段:类型 -> 主字段
_IMAGE_TYPES = ("image",)
_VIDEO_TYPES = ("media",)


def _pending(session, chat_id: str | None, limit: int) -> list:
    """取缺落盘的资源消息(image 缺 image_url / media 缺 file_url)。"""
    stmt = (
        select(Message)
        .where(Message.message_type.in_(("image", "media")))
        .order_by(Message.id)
        .limit(limit)
    )
    if chat_id:
        stmt = stmt.where(Message.chat_id == chat_id)

    out = []
    for m in session.execute(stmt).scalars().all():
        raw = m.raw if isinstance(m.raw, dict) else {}
        if m.message_type == "media":
            if not raw.get("file_url"):
                out.append(m)
        else:
            if not raw.get("image_url"):
                out.append(m)
    return out


def _fill(m: Message, dry_run: bool) -> bool:
    """补一条资源消息的资源 URL。返回是否补齐成功。"""
    raw = m.raw if isinstance(m.raw, dict) else {}
    if m.message_type == "media":
        ms._resolve_video(raw, m.message_id or "")
        filled = bool(raw.get("file_url"))
    else:
        ms._resolve_image(raw, m.message_id or "")
        filled = bool(raw.get("image_url"))

    if not filled:
        return False
    m.raw = raw
    return True


def backfill(dry_run: bool, chat_id: str | None, limit: int) -> int:
    """回填资源 URL,返回成功更新的条数。"""
    with SessionLocal() as session:
        pending = _pending(session, chat_id, limit)
        if not pending:
            logger.info("没有待回填的资源消息(或全部已落盘)")
            return 0

        logger.info("待回填资源消息 %d 条, dry_run=%s", len(pending), dry_run)
        ok = 0
        for m in pending:
            raw = m.raw if isinstance(m.raw, dict) else {}
            tag = "media" if m.message_type == "media" else "image"
            key = raw.get("file_key") or raw.get("image_key") or ""
            if _fill(m, dry_run):
                ok += 1
                # 找到补齐后的 URL 打印
                new_url = raw.get("file_url") or raw.get("image_url") or ""
                logger.info("[%d/%d] %s %s -> %s", ok, len(pending), tag, key, new_url)
                if not dry_run:
                    session.add(m)
            else:
                logger.warning("回填失败,保留占位 %s=%s", tag, key)

        if not dry_run:
            session.commit()
            logger.info("已更新 %d 条资源消息", ok)
        else:
            logger.info("[dry-run] 将更新 %d 条资源消息", ok)
        return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="回填图片/视频资源 URL(下载上传七牛)")
    ap.add_argument("--chat", default=None, help="仅回填指定 chat_id")
    ap.add_argument("--limit", type=int, default=500, help="最多扫描条数(默认500)")
    ap.add_argument("--dry-run", action="store_true", help="只预览,不改库")
    args = ap.parse_args()

    log_file = "data/backfill_images.log" if not args.dry_run else ""
    setup_logging(level="INFO", log_file=log_file)

    n = backfill(args.dry_run, args.chat, args.limit)
    print(f"回填完成: 更新 {n} 条资源消息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
