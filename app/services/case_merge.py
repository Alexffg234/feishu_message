"""案例软合并(阶段三 P1-A):同一案例的会话用 case_id 关联,可回滚。

与 group_merge.py 的物理合并(改 root/conversation_id、删源对话行)互补:
- 物理合并 = 不可逆,当下游真正要合并时用;
- 本模块 = 可逆关联,只写 conversations.case_id,不删行、不改 root、不搬消息,
  误合可一键解绑(case_id 归 0)。

并查集不变式(consistent):
- case_id = 该会话所属案例组的「代表会话 id」,0 = 独立案例;
- 一个案例组 ≤ 1 个代表,且代表会话的 case_id 自指向自身(= 自身 id);
  其余成员 case_id = 代表 id;故「case_id == rep.id」即可命中全组(含代表)。
- 代表 = 组内最早会话(时间升序首条);每次软合并后整组重新对齐到全局最早代表,
  杜绝 A→B→C 链式漂移。
- 只有真正发生了(或正要发生)关联的代表才自指向;从未合并的独立会话保持 0。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation

logger = logging.getLogger(__name__)


def _get(session: Session, conversation_id: int) -> Conversation:
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        raise ValueError(f"会话不存在: {conversation_id}")
    return conv


def resolve_case_root(session: Session, conversation_id: int) -> Conversation:
    """沿 case_id 链向上找该会话所在案例组的代表(路径压缩)。

    case_id=0(独立)或自指向(自身即代表)时返回自身;链异常时返回最后可达者。
    """
    conv = _get(session, conversation_id)
    seen: set[int] = set()
    cur = conv
    while cur.case_id and cur.case_id != cur.id and cur.case_id not in seen:
        seen.add(cur.id)
        parent = session.get(Conversation, cur.case_id)
        if parent is None:
            break
        cur = parent
        if cur.id == conv.id:
            break  # 环(理论上不会出现),防死循环
    return cur


def _members_of(session: Session, chat_id: str, rep: Conversation) -> list[Conversation]:
    """案例组全部成员:代表(自指向) + case_id==rep.id 的成员。"""
    return list(
        session.scalars(
            select(Conversation).where(
                Conversation.chat_id == chat_id,
                Conversation.case_id == rep.id,
            )
        ).all()
    )


def align_case(
    session: Session,
    chat_id: str,
    case_id: int,
    root: Conversation,
    commit: bool = False,
) -> int:
    """把既有案例组(case_id==case_id)全部成员对齐到 root.id 代表的案例组。

    用于整组重定向(如把碎片组并入更早案例)。返回受影响会话数。
    root 自身也补齐自指向,保证「代表=组成员」不变式。幂等,不 commit(除非 commit=True)。
    """
    changed = 0
    if root.case_id != root.id:
        root.case_id = root.id
        changed += 1
    rows = list(
        session.scalars(
            select(Conversation).where(
                Conversation.chat_id == chat_id,
                Conversation.case_id == case_id,
            )
        ).all()
    )
    for c in rows:
        if c.case_id != root.id:
            c.case_id = root.id
            changed += 1
    if commit:
        session.commit()
    return max(changed, 1 if rows else 0)


def soft_link_cases(
    session: Session,
    chat_id: str,
    src_convid: int,
    tgt_convid: int,
) -> bool:
    """把 src 会话(及其所在案例组)软合并进 tgt 会话(同一案例),返回是否发生关联。

    语义:
    - 各自解析到代表;若已是同组,幂等返回 False;
    - 目标 = 两个代表中「更早」者(按代表 id 升序;plan_stitches 已保证 tgt 早,此处兜底);
    - 另一组成员全部 case_id 对齐到目标代表 id;目标代表自指向补齐;
    - 只改 case_id,不删行/不改 root/不搬消息;
    - 不 commit,由调用方统一提交。
    """
    if src_convid == tgt_convid:
        return False
    src_root = resolve_case_root(session, src_convid)
    tgt_root = resolve_case_root(session, tgt_convid)
    if src_root.id == tgt_root.id:
        return False  # 已在同一案例组(幂等)

    # 目标 = 更早的代表(按 id 升序≈时间升序)
    if src_root.id < tgt_root.id:
        root, other = src_root, tgt_root
    else:
        root, other = tgt_root, src_root

    # 目标代表补齐自指向(若之前是独立会话 case_id=0)
    if root.case_id != root.id:
        root.case_id = root.id
    # 另一组全体成员对齐到目标代表
    changed = 0
    for c in _members_of(session, chat_id, other):
        if c.case_id != root.id:
            c.case_id = root.id
            changed += 1
    if other.case_id != root.id:
        other.case_id = root.id
        changed += 1
    logger.info(
        "案例软合并 %s: 会话组 #%d 并入 #%d (关联 %d 条 case_id)",
        chat_id, other.id, root.id, changed,
    )
    return True


def unlink_case(session: Session, chat_id: str, case_id: int, commit: bool = False) -> int:
    """解绑案例组:chat_id 内共享该 case_id 的全部会话归 0。

    返回受影响会话数。纯逻辑回滚,行/消息/root 一律不动。
    """
    rows = list(
        session.scalars(
            select(Conversation).where(
                Conversation.chat_id == chat_id,
                Conversation.case_id == case_id,
            )
        ).all()
    )
    n = 0
    for c in rows:
        c.case_id = 0
        n += 1
    if commit:
        session.commit()
    return n


def merge_case(session: Session, chat_id: str, case_id: int) -> int:
    """管理员确认后:把案例组物理合并为一条对话(合并进最早会话为根)。

    返回被并入的源会话数。执行后该组 case_id 归 0(已物理合并,不再软关联)。
    复用 merge_groups:改 root/conversation_id、删源对话行,消息全部归到最早根。
    不块标记(同一案例无论据画横杠),保持缝合"不断句"语义。
    """
    from app.services.group_merge import merge_groups

    rows = list(
        session.scalars(
            select(Conversation).where(
                Conversation.chat_id == chat_id,
                Conversation.case_id == case_id,
            )
        ).all()
    )
    rows.sort(key=lambda c: (c.id, c.root_message_id))  # 最早在前
    if len(rows) < 2:
        # 单条或空组的确认合并:只把 case_id 清掉(无需物理合并)
        for c in rows:
            c.case_id = 0
        session.commit()
        return 0
    target_root = rows[0].root_message_id
    target_conv = rows[0]
    merged = 0
    for src in rows[1:]:
        if merge_groups(
            session, chat_id, src.root_message_id, target_root,
            target_conv.topic or "", mark_block_start=False,
        ):
            merged += 1
    # 残留(可能 merge_groups 因缺数据跳过)的 case_id 归 0
    for c in session.query(Conversation).filter(
        Conversation.chat_id == chat_id, Conversation.case_id == case_id
    ).all():
        c.case_id = 0
    session.commit()
    return merged
