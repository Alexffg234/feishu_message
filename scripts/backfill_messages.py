"""回补拉取漏采消息。

通过飞书「获取会话历史消息」接口(im/v1/messages)按时间窗拉取群消息,
逐条走正常入库管道(解析/媒体下载/对话挂组/异步主题分类),按 message_id 防重。

用途:事件回调期间服务重启/超时导致漏采的消息回补。

用法(项目根目录下,用服务器 venv 运行):
    python scripts/backfill_messages.py --chat oc_xxx --start "2026-10-19 14:39:00" --end "2026-10-19 15:00:00"
    python scripts/backfill_messages.py --chat oc_xxx --start "..." --end "..." --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

# 兼容 lark-oapi 新旧版本:新版 ListMessagesRequest / 旧版 ListMessageRequest
try:
    from lark_oapi.api.im.v1 import ListMessagesRequest as _ListReq
except ImportError:  # noqa: F401
    from lark_oapi.api.im.v1 import ListMessageRequest as _ListReq

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.dao.message_repo import MessageRepo
from app.services.message_service import MessageService
from app.services.topic_service import get_topic_service

logger = logging.getLogger("backfill_messages")


def _build_client() -> lark.Client:
    settings = get_settings()
    if not (settings.app_id and settings.app_secret):
        raise SystemExit("缺少 app_id/app_secret 配置,无法调飞书接口")
    return lark.Client.builder().app_id(settings.app_id).app_secret(settings.app_secret).build()


def _fetch_messages(client: lark.Client, chat_id: str, start_ts: int, end_ts: int):
    msgs = []
    page_token = None
    while True:
        builder = (
            _ListReq.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .start_time(str(start_ts))
            .end_time(str(end_ts))
            .page_size(50)
        )
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.im.v1.message.list(builder.build())
        if resp is None or not resp.success():
            raise SystemExit(
                f"拉取消息失败 code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '?')}"
            )
        # 兼容新旧 SDK:新版顶层 resp.messages / 旧版 resp.data.items
        if hasattr(resp, "messages"):
            items, has_more, token = resp.messages, resp.has_more, resp.page_token
        else:
            data = resp.data
            items, has_more, token = data.items, data.has_more, data.page_token
        msgs.extend(list(items or []))
        if not has_more:
            break
        page_token = token
    return msgs


def _to_event(m) -> P2ImMessageReceiveV1:
    """把 im/v1/messages 的 Message 映射成 im.message.receive_v1 事件对象。"""
    sender = m.sender
    sid: dict = {}
    if sender is not None:
        id_type = getattr(sender, "id_type", None) or "open_id"
        sid = {id_type: sender.id} if sender.id else {}
    mentions = []
    for x in m.mentions or []:
        mentions.append({"key": x.key, "name": x.name or ""})
    body = m.body
    content = (body.content if body is not None else "") or ""
    try:
        create_time = int(m.create_time)
    except (TypeError, ValueError):
        create_time = 0
    event = {
        "sender": {
            "sender_id": sid,
            "sender_type": getattr(sender, "sender_type", None) or "",
        },
        "message": {
            "message_id": m.message_id or "",
            "chat_id": m.chat_id or "",
            "message_type": m.msg_type or "",
            "create_time": create_time,
            "content": content,
            "parent_id": m.parent_id or "",
            "root_id": m.root_id or "",
            "mentions": mentions,
        },
    }
    return P2ImMessageReceiveV1({"event": event})


def backfill(chat_id: str, start: datetime, end: datetime, dry_run: bool) -> None:
    settings = get_settings()
    client = _build_client()
    msgs = _fetch_messages(client, chat_id, int(start.timestamp()), int(end.timestamp()))
    if not msgs:
        print(f"{chat_id} {start:%H:%M}~{end:%H:%M} 无消息")
        return
    msgs.sort(key=lambda m: int(m.create_time or 0))
    print(f"拉取到 {len(msgs)} 条消息({start:%H:%M}~{end:%H:%M})")

    with SessionLocal() as session:
        repo = MessageRepo(session)
        service = MessageService(
            session,
            allowed_chat_ids=settings.allowed_chat_id_set,
            topic_scheduler=get_topic_service().schedule_group,
        )
        new_cnt = skip_cnt = 0
        for m in msgs:
            mid = m.message_id
            ts = datetime.fromtimestamp(int(m.create_time or 0) / 1000).strftime("%H:%M:%S")
            if repo.get_by_message_id(chat_id, mid) is not None:
                print(f"  [跳过·已存在] {ts} {m.msg_type} {mid}")
                skip_cnt += 1
                continue
            if dry_run:
                print(f"  [dry-run 将入库] {ts} {m.msg_type} {mid}")
                new_cnt += 1
                continue
            try:
                ok = service.handle_receive_event(_to_event(m))
                if ok:
                    print(f"  [入库] {ts} {m.msg_type} {mid}")
                    new_cnt += 1
                else:
                    print(f"  [失败] {ts} {m.msg_type} {mid}")
            except Exception:  # noqa: BLE001
                logger.exception("回补入库异常 %s", mid)
                session.rollback()
        print(f"完成: 新入库 {new_cnt} 条, 已存在跳过 {skip_cnt} 条 {'(dry-run 未落库)' if dry_run else ''}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="回补拉取漏采消息")
    p.add_argument("--chat", required=True, help="群 chat_id")
    p.add_argument("--start", required=True, help='开始时间,如 "2026-10-19 14:39:00"')
    p.add_argument("--end", required=True, help='结束时间,如 "2026-10-19 15:00:00"')
    p.add_argument("--dry-run", action="store_true", help="只预览不落库")
    args = p.parse_args()
    backfill(
        args.chat,
        datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S"),
        datetime.strptime(args.end, "%Y-%m-%d %H:%M:%S"),
        args.dry_run,
    )


if __name__ == "__main__":
    main()
