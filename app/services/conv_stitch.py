"""会话缝合:LLM 判定同群相邻会话是否同一问题处理生命周期,是则合并。

与同标签归并(group_merge)互补:同标签归并依赖 LLM 标签,标签缺失或
垃圾标签(如"好的")时失效;本模块直接把相邻会话的代表消息喂给 LLM,
按「提出问题 → 排查问题 → 多轮对话 → 给出解决方案」的生命周期判定
是否同一处理过程,再复用 merge_groups 合并(最早会话保留为根)。
不依赖标签锚点,幂等可重跑。

窗口策略:按时间升序取相邻 _BATCH_SIZE 个会话送 LLM,窗口步进
_BATCH_STEP(重叠 1 个,边界会话会被两个窗口共同判定),每个窗口输出
「应合并的序号集合」,仅在该窗口内部解析合并对(不做跨窗传递闭包,
避免 LLM 偶发误判经闭包链式放大成巨型过度合并)。

安全护栏(LLM 误判防护):
- 时间间隙:源会话开始 - 目标会话结束 > _MAX_GAP_MS(6 小时)不合并,
  问题处理生命周期极少跨这么长;
- 标签冲突:双方都有非空 topic 且不同时不合并(已有确定标签是强反证)。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.message import Message
from app.services.group_merge import merge_groups
from app.services.llm import LLMError

logger = logging.getLogger(__name__)

# 每批送 LLM 的相邻会话数上限
_BATCH_SIZE = 5
# 窗口步进(与 _BATCH_SIZE 重叠 1 个,保证边界会话被相邻两窗共同判定)
_BATCH_STEP = 4
# 每会话代表消息条数上限(首条+末条+最长)
_REPR_CHARS = 60
# 时间间隙护栏:源会话开始 - 目标会话结束 超过该值不合并(问题生命周期极少跨这么长)
_MAX_GAP_MS = 6 * 3600 * 1000

_SYSTEM_PROMPT = (
    "你是群聊对话缝合判定器。以下是同一群内按时间顺序相邻的若干会话,每个会话给出"
    "主题标签、消息条数与代表消息(首条/末条/最长)。\n"
    "请判定哪些会话属于同一问题处理过程。\n"
    "规则:\n"
    "1. 提出问题、排查问题、多轮对话、最后给出解决方案属于同一会话;即使发言人不同、"
    "措辞不同、间隔几十分钟,只要都在处理同一问题(同一台机器/同一事项/同一用户)就必须合并。\n"
    "2. 全新问题(不同机器、不同故障现象、不同事项)不与前面的问题合并。\n"
    "3. 只输出应合并的会话序号(从1开始),逗号分隔,如 1,2,3;无需合并时只输出 无。"
    "不要输出其他文字。"
)


def _fmt_time(create_time: str) -> str:
    try:
        return datetime.fromtimestamp(int(create_time) / 1000).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return create_time or "-"


def _repr_lines(rows: list[Message]) -> str:
    """取代表消息:首条+末条+最长(去重,按时间排序,截断)。"""
    reps = [rows[0], rows[-1]]
    longest = max(rows, key=lambda m: len((m.text or "").strip()))
    if all(longest.id != r.id for r in reps):
        reps.append(longest)
    seen = set()
    lines = []
    for r in sorted(reps, key=lambda m: m.id):
        if r.id in seen:
            continue
        seen.add(r.id)
        text = (r.text or "").strip() or f"({r.message_type or '非文本'})"
        lines.append(f"[{_fmt_time(r.create_time)}] {r.sender_id or '?'}: {text[:_REPR_CHARS]}")
    return "\n".join(lines)


def _parse_stitch_result(raw: str, n: int) -> set[int]:
    """解析 LLM 输出为 1 基应合并序号集合;越界/非法/"无" 忽略。"""
    out: set[int] = set()
    for tok in (raw or "").replace("，", ",").replace(" ", "").split(","):
        tok = tok.strip()
        if not tok or tok == "无":
            continue
        try:
            idx = int(tok)
        except ValueError:
            continue
        if 1 <= idx <= n:
            out.add(idx)
    return out


def _pair_allowed(src: Conversation, tgt: Conversation, chat_id: str) -> bool:
    """合并对护栏:时间间隙超限或双方 topic 冲突时拒绝。"""
    gap_ms = src.started_at_ms - tgt.last_message_at_ms
    if gap_ms > _MAX_GAP_MS:
        logger.info(
            "会话缝合跳过 %s: #%d -> #%d 时间间隙 %.1f 小时超过 %.0f 小时",
            chat_id, src.id, tgt.id, gap_ms / 3600000, _MAX_GAP_MS / 3600000,
        )
        return False
    if src.topic and tgt.topic and src.topic != tgt.topic:
        logger.info(
            "会话缝合跳过 %s: #%d -> #%d 主题冲突(%s vs %s)",
            chat_id, src.id, tgt.id, src.topic, tgt.topic,
        )
        return False
    return True


def _list_conversations(session: Session, chat_id: str) -> list[tuple[Conversation, list[Message]]]:
    """该群全部会话(按首条消息时间升序),空会话跳过。"""
    convs = (
        session.query(Conversation)
        .filter(Conversation.chat_id == chat_id)
        .order_by(Conversation.id.asc())
        .all()
    )
    result: list[tuple[Conversation, list[Message]]] = []
    for c in convs:
        rows = (
            session.query(Message)
            .filter(Message.conversation_id == c.id)
            .order_by(Message.id.asc())
            .all()
        )
        if rows:
            result.append((c, rows))
    result.sort(key=lambda cr: cr[1][0].id)
    return result


def plan_stitches(
    session: Session,
    chat_id: str,
    llm,
) -> list[tuple[int, int]]:
    """规划会话缝合并对,返回 [(源会话id, 目标会话id)],目标=同组最早会话。

    每个窗口的合并集合独立解析(不做跨窗传递闭包,防链式放大过度合并),
    每个 (源,目标) 对再过时间间隙/主题冲突护栏。
    只读不改库,供 dry-run 预览;llm 为空或会话不足 2 个时返回空。
    """
    if llm is None:
        return []
    convs = _list_conversations(session, chat_id)
    if len(convs) < 2:
        return []

    claimed: set[int] = set()  # 本轮已并入他组的会话下标,防重复合并
    pairs: list[tuple[int, int]] = []
    for start in range(0, len(convs), _BATCH_STEP):
        window = convs[start : start + _BATCH_SIZE]
        if len(window) < 2:
            continue
        blocks = []
        for i, (c, rows) in enumerate(window, 1):
            blocks.append(
                f"会话{i} (主题: {c.topic or '无'} {len(rows)}条)\n{_repr_lines(rows)}"
            )
        try:
            raw = llm.chat(_SYSTEM_PROMPT, "\n\n".join(blocks))
        except LLMError as e:
            logger.warning("会话缝合 LLM 调用失败 %s 窗口@%d: %s", chat_id, start, e)
            continue
        idxs = _parse_stitch_result(raw, len(window))
        logger.debug(
            "会话缝合 %s 窗口@%d 输出 %r -> 合并组 %s", chat_id, start, raw, sorted(idxs)
        )
        if len(idxs) < 2:
            continue
        members = [start + i - 1 for i in sorted(idxs)]  # 下标升序=时间升序
        target = members[0]
        if target in claimed:
            continue
        for g in members[1:]:
            if g in claimed:
                continue
            src_c, tgt_c = convs[g][0], convs[target][0]
            if not _pair_allowed(src_c, tgt_c, chat_id):
                continue
            pairs.append((src_c.id, tgt_c.id))
            claimed.add(g)
        claimed.add(target)
    return pairs


def stitch_chat(
    session: Session,
    chat_id: str,
    llm,
) -> list[tuple[int, int]]:
    """执行会话缝合,返回实际合并的 (源会话id, 目标会话id) 列表。"""
    done: list[tuple[int, int]] = []
    for src_id, tgt_id in plan_stitches(session, chat_id, llm):
        src = session.get(Conversation, src_id)
        tgt = session.get(Conversation, tgt_id)
        if src is None or tgt is None:
            continue
        if merge_groups(
            session, chat_id, src.root_message_id, tgt.root_message_id, tgt.topic or ""
        ):
            done.append((src_id, tgt_id))
    return done
