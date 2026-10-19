"""会话拆分:把被同主题归并混在一起的对话拆回多个独立对话。

merge_groups 的逆操作:同标签归并可能把多个不同问题混进一个对话(标签
打错时尤其明显)。拆分按事先审阅好的分配表(每个新组一组 message_ids)
把消息重新挂组、重建对话行、迁移回复边。

回复边跟随父消息(list_tree 以父为锚点渲染):跨组边落在父所在对话,
子消息不在该对话消息集里,渲染时被自然过滤,不会出现在任何一方树中;
若边留在子一侧,该子消息会因「是某边 child 但父不在本对话」而整条消失。

安全约定:
- 计划必须精确覆盖该对话的全部消息:不重、不漏、不含对话外的消息
- keep 组的 root 必须等于该对话当前 root_message_id(对话行保留)
- 新组 root 必须在本组消息内(新对话行以其为根)
- 默认 dry-run:只校验并报告;apply=True 才落库
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dao.conversation_repo import ConversationRepo
from app.models.conversation import Conversation, MessageLink
from app.models.message import Message

logger = logging.getLogger(__name__)


def split_conversation(
    session: Session,
    chat_id: str,
    conversation_id: int,
    groups: list[dict[str, Any]],
    apply: bool = False,
) -> dict:
    """按计划拆分指定对话,返回报告(apply=False 时不落库)。

    groups 每项:{"root_message_id": str, "topic": str,
                 "message_ids": [str, ...], "keep": bool(可选)}
    校验失败抛 ValueError。
    """
    if not groups:
        raise ValueError("拆分计划为空")
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.chat_id != chat_id:
        raise ValueError(f"对话不存在或 chat 不匹配: id={conversation_id}")

    msgs = session.scalars(
        select(Message).where(Message.conversation_id == conversation_id)
    ).all()
    by_mid = {m.message_id: m for m in msgs}

    plan: list[dict] = []
    seen: set[str] = set()
    for g in groups:
        root = g.get("root_message_id") or ""
        mids = list(g.get("message_ids") or [])
        topic = g.get("topic") or ""
        keep = bool(g.get("keep"))
        if root not in by_mid:
            raise ValueError(f"组根不在该对话中: {root}")
        if root not in mids:
            raise ValueError(f"组根必须在本组消息内: {root}")
        dup = seen & set(mids)
        if dup:
            raise ValueError(f"消息被多个组认领: {sorted(dup)[:5]}")
        seen |= set(mids)
        if keep and root != conv.root_message_id:
            raise ValueError(f"keep 组根必须等于当前对话根: {conv.root_message_id}")
        plan.append({"root": root, "topic": topic, "mids": mids, "keep": keep})

    missing = set(by_mid) - seen
    if missing:
        raise ValueError(f"计划漏了 {len(missing)} 条消息,如: {sorted(missing)[:5]}")
    extra = seen - set(by_mid)
    if extra:
        raise ValueError(f"计划含对话外消息: {sorted(extra)[:5]}")

    conv_ids: list[int] = []
    if apply:
        convo = ConversationRepo(session)
        for p in plan:
            if p["keep"]:
                conv_ids.append(conv.id)
                if p["topic"]:
                    conv.topic = p["topic"]
            else:
                root_msg = by_mid[p["root"]]
                new_conv = convo.create(chat_id, p["root"], root_msg.sender_id)
                if p["topic"]:
                    new_conv.topic = p["topic"]
                conv_ids.append(new_conv.id)
        session.flush()
        for i, p in enumerate(plan):
            for mid in p["mids"]:
                m = by_mid[mid]
                m.root_message_id = p["root"]
                m.conversation_id = conv_ids[i]
                # 同步组内消息标签:否则残留的旧标签会让新消息分类时
                # 把新组误并回旧标签的组(classify_group 继承组内已有 topic)
                if p["topic"]:
                    m.topic = p["topic"]
        mid_to_conv = {
            mid: conv_ids[i] for i, p in enumerate(plan) for mid in p["mids"]
        }
        links = session.scalars(
            select(MessageLink).where(MessageLink.conversation_id == conv.id)
        ).all()
        for link in links:
            parent_conv = mid_to_conv.get(link.parent_message_id)
            if parent_conv is not None:
                link.conversation_id = parent_conv
        for cid in dict.fromkeys(conv_ids):
            convo.update_group_stats(cid)
        session.commit()
    else:
        conv_ids = [0] * len(plan)

    report = {
        "conversation_id": conversation_id,
        "chat_id": chat_id,
        "applied": apply,
        "groups": [
            {
                "root": p["root"],
                "topic": p["topic"],
                "count": len(p["mids"]),
                "conversation_id": conv_ids[i],
            }
            for i, p in enumerate(plan)
        ],
    }
    logger.info(
        "会话拆分 %s id=%d %s: %d 组共 %d 条",
        chat_id,
        conversation_id,
        "apply" if apply else "dry-run",
        len(plan),
        len(by_mid),
    )
    return report
