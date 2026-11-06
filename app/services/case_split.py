"""问题单元切分:把混了多个问题的对话切成「一个问题一个对话」。

为什么需要它
------------
同主题归并(merge_groups)按标签把多次求助并进同一对话,一组里常混 3~5 个
不同问题。此时无论 LLM 怎么选,单标签都必然覆盖不全——审计里就有对话把
4 天内的 4 件不相关事并在一起,摘要还丢了关键线索。根因不在 prompt,
而在「一组 = 一个问题」这个建模错误,所以要在标签之前先把组切开。

切点判据(全部规则,零 LLM 成本)
--------------------------------
1. 设备标识变化:messages.case_key 两侧都非空且不同 → 必是不同案例,必切
2. 显式块起点:messages.block_start=1(归并打标时标记的新问题块起点)
3. 时间断档:相邻消息间隔 > gap_sec(默认 2 小时)→ 上一轮求助已结束
三条取并集。切出来的片若全是客套句(「好的谢谢」)则并回上一片,避免噪声单元。

与 case_id 的关系(两层模型)
----------------------------
- 问题单元 = conversation:物理边界,一个单元一个标签,一次只讲一件事
- 案例组 = conversations.case_id:软关联,用于跨单元看「同一台设备的全部往来」
切分**不继承** case_id:新单元从 0 开始,由缝合/backfill_case_id 重新判定。
否则在案例视图里切了等于没切(又被 case_id 粘回去)。

落地复用
--------
执行层沿用 conversation_split.split_conversation:它负责计划校验(不重不漏
不含外部消息)、重挂消息与对话行、迁移回复边、重算统计。
跨切点的回复边留在父消息所在对话;子消息在两侧都按根渲染
(list_tree 对「父不在本对话」的消息按根处理),不会丢消息。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.conversation_split import split_conversation
from app.services.stage import is_filler_only, update_group_stage

logger = logging.getLogger(__name__)

# 时间断档阈值(秒):超过即认为上一轮求助已结束、后面是新问题。
# 2 小时是「一次会话不会中断这么久」与「隔半天/隔天再问」的折中。
# 宁可切碎:切碎只是多几个单元,切不开则一个标签要覆盖多个问题,事后无法补救。
_DEFAULT_GAP_SEC = 2 * 3600

# 切点原因(进报告用,便于统计哪个信号真正起作用)
CUT_START = "start"
CUT_CASE_KEY = "case_key"
CUT_BLOCK = "block_start"
CUT_GAP = "gap"

_MISSING_TS = 1 << 62


def _ts(create_time: str) -> int | None:
    """毫秒时间戳字符串 -> 毫秒 int;不可解析返回 None(该边界不判时间)。"""
    t = (create_time or "").strip()
    if not t.isdigit():
        return None
    v = int(t)
    # 兼容秒级时间戳
    return v * 1000 if 0 < v < 10**12 else v


def _order_key(m: Message) -> tuple[int, int]:
    """时间正序;时间缺失排最后,同时间按自增 id 保证稳定。"""
    ts = _ts(m.create_time)
    return (ts if ts is not None else _MISSING_TS, int(m.id or 0))


def _cut_reason(prev: Message, cur: Message, gap_sec: int) -> str:
    """判断 cur 是否应作为新问题单元的起点;返回切点原因,不切返回 ""。

    优先级:设备号 > 显式块起点 > 时间断档(越确定的信号越先认)。
    """
    k_prev = (getattr(prev, "case_key", "") or "").strip()
    k_cur = (getattr(cur, "case_key", "") or "").strip()
    if k_prev and k_cur and k_prev != k_cur:
        return CUT_CASE_KEY
    if int(getattr(cur, "block_start", 0) or 0) == 1:
        return CUT_BLOCK
    a, b = _ts(prev.create_time), _ts(cur.create_time)
    if a is not None and b is not None and (b - a) > gap_sec * 1000:
        return CUT_GAP
    return ""


def _is_noise_piece(rows: list[Message]) -> bool:
    """整片只有客套/确认,且不含非文本消息(图片/视频/文件本身带信息)。"""
    if any((m.message_type or "text") != "text" for m in rows):
        return False
    return all(is_filler_only(m.text or "") for m in rows)


def _absorb_noise(pieces: list[dict]) -> list[dict]:
    """把纯客套片并回上一片。

    第一片永远保留原样:对话根消息在片内,组根必须等于对话根,
    片被并走会让原对话行无处安放。
    """
    if len(pieces) < 2:
        return pieces
    out = [pieces[0]]
    for p in pieces[1:]:
        if _is_noise_piece(p["rows"]):
            out[-1]["rows"].extend(p["rows"])
            continue
        out.append(p)
    return out


def plan_pieces(
    rows: Iterable[Message], gap_sec: int = _DEFAULT_GAP_SEC
) -> list[dict]:
    """把一组消息按时间切成问题单元,返回 [{"rows": [...], "reason": 切点原因}]。

    reason=CUT_START 表示该片是对话第一片(无切点)。纯函数,不访问 DB,便于测试。
    """
    ordered = sorted(rows, key=_order_key)
    if not ordered:
        return []
    pieces: list[dict] = [{"rows": [ordered[0]], "reason": CUT_START}]
    prev = ordered[0]
    for m in ordered[1:]:
        reason = _cut_reason(prev, m, gap_sec)
        if reason:
            pieces.append({"rows": [m], "reason": reason})
        else:
            pieces[-1]["rows"].append(m)
        prev = m
    return _absorb_noise(pieces)


def build_split_groups(conv_root_message_id: str, pieces: list[dict]) -> list[dict]:
    """把切分结果转成 split_conversation 需要的 groups 计划。

    每片的根取片内最早消息;含当前对话根的那片标 keep(沿用原对话行)。
    topic 一律传空:各单元切分后各自重新分类,不继承混在一起的旧标签。
    """
    groups: list[dict] = []
    for p in pieces:
        rows = p["rows"]
        root = next(
            (m.message_id for m in rows if m.message_id == conv_root_message_id), ""
        )
        root = root or (rows[0].message_id or "")
        if not root:
            raise ValueError("问题单元内消息缺少 message_id,无法作为组根")
        groups.append(
            {
                "root_message_id": root,
                "topic": "",
                "message_ids": [m.message_id for m in rows],
                "keep": root == conv_root_message_id,
                "reason": p["reason"],
            }
        )
    return groups


def split_conversation_by_case(
    session: Session,
    chat_id: str,
    conversation_id: int,
    gap_sec: int = _DEFAULT_GAP_SEC,
    apply: bool = False,
) -> dict:
    """把一个对话按问题单元切开。apply=False 只出报告,不落库。

    返回 report:split=是否发生切分、pieces=单元数、groups=每组根/条数/对话 id、
    cut_reasons=各片切点原因。校验失败抛 ValueError(与 split_conversation 一致)。
    """
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.chat_id != chat_id:
        raise ValueError(f"对话不存在或 chat 不匹配: id={conversation_id}")

    rows = ConversationRepo(session).list_by_conversation(conversation_id)
    if not rows:
        return {
            "conversation_id": conversation_id,
            "applied": False,
            "split": False,
            "pieces": 0,
            "messages": 0,
            "reason": "对话无消息",
        }

    pieces = plan_pieces(rows, gap_sec)
    if len(pieces) < 2:
        return {
            "conversation_id": conversation_id,
            "applied": False,
            "split": False,
            "pieces": 1,
            "messages": len(rows),
            "reason": "无可切点",
        }

    groups = build_split_groups(conv.root_message_id, pieces)
    if not any(g["keep"] for g in groups):
        # 对话根不在消息集里:切下去原对话行会变成空壳,直接拒绝
        raise ValueError(f"切分计划丢失对话根 {conv.root_message_id}")

    report = split_conversation(session, chat_id, conversation_id, groups, apply=apply)
    report.update(
        {
            "split": True,
            "messages": len(rows),
            "pieces": len(groups),
            "cut_reasons": [g["reason"] for g in groups],
        }
    )
    if not apply:
        return report

    # 落库后:新单元的标签是从「混合组」继承来的,一律清空等重新分类;
    # keep 组保留原标签(它承接的正是组首那个问题,旧标签多半本就指向它)。
    repo = MessageRepo(session)
    for g in report["groups"]:
        if g["root"] == conv.root_message_id:
            continue
        repo.set_topic_for_group(chat_id, g["root"], "")
        repo.set_topic_conf_for_group(chat_id, g["root"], 0.0)
    session.commit()

    # 阶段是纯规则、跟着组内文本走:切开后每单元各自重算
    for g in report["groups"]:
        update_group_stage(session, chat_id, g["root"])

    logger.info(
        "问题单元切分 %s #%d: %d 条 -> %d 个单元(%s)",
        chat_id,
        conversation_id,
        len(rows),
        len(groups),
        ",".join(report["cut_reasons"]),
    )
    return report
