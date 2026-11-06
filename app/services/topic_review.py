"""人工复核闭环:低置信队列 -> 人工定标 -> 回写库 + 反哺别名表 -> gold set。

为什么要这一层:闭集 + 召回 + 相似度兜底都只能把「模型能判对的」判对,剩下
那些判不对的必须有人看一遍,而且**看一遍的价值要能沉淀下来**,否则同一类错
会一直错下去。沉淀路径:

    人工把一组的标签改对
      -> 写回 messages/conversations(置 topic_source='human',置信度 1.0)
      -> 若原来是另一个非标准标签,写进 topic_aliases(下次 normalize 直接收敛)
      -> 该组进入 gold set(评估判别准确率的唯一可信基线)

一条硬护栏:**原标签是标准标签时绝不写别名**。把「屏幕闪烁」映射成「画面
模糊类问题」会让以后所有「屏幕闪烁」都被改写——一次人工纠正毒化整个标签
体系,这个代价比漏写一条别名大得多。
"""
from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.models.topic_alias import TopicAlias
from app.services.label_norm import clean_label, normalize_label

logger = logging.getLogger(__name__)

# 队列里「低置信」的判定线:低于此值交给人工(与 /api/topic_metrics 分档一致)
DEFAULT_LOW_CONF = 0.5
# 人工给的标签也先过一遍相似度兜底,把「画面模糊」收敛到标准名再校验闭集
_HUMAN_SIM_THRESHOLD = 0.65

SOURCE_HUMAN = "human"
SOURCE_LLM = "llm"


# ---------------------------------------------------------------- 队列
def list_review_queue(
    session: Session,
    chat_id: str | None = None,
    limit: int = 100,
    low_conf: float = DEFAULT_LOW_CONF,
    include_human: bool = False,
) -> dict:
    """待人工复核队列:未分类组 + 低置信组,附样本文本供快速判断。

    返回 {"total": 队列总长(不受 limit 截断), "items": [...]}。
    """
    repo = MessageRepo(session)
    items = repo.list_review_roots(
        chat_id=chat_id, limit=limit, low_conf=low_conf, include_human=include_human
    )
    total = repo.count_review_roots(
        chat_id=chat_id, low_conf=low_conf, include_human=include_human
    )
    # 样例按群分组批量取(一次查询一个群),避免每组一次查询
    by_chat: dict[str, list[str]] = {}
    for it in items:
        by_chat.setdefault(it["chat_id"], []).append(it["root_message_id"])
    samples: dict[tuple[str, str], list[str]] = {}
    for chat, roots in by_chat.items():
        for root, texts in repo.get_group_samples(chat, roots).items():
            samples[(chat, root)] = texts
    for it in items:
        it["samples"] = samples.get((it["chat_id"], it["root_message_id"]), [])
        it["reason"] = "unclassified" if not it["labeled"] else "low_conf"
    return {"total": total, "items": items}


# ---------------------------------------------------------------- 定标
def apply_review(
    session: Session,
    chat_id: str,
    root_message_id: str,
    label: str,
    *,
    standards: list[str] | None = None,
    allow_new: bool = False,
    propagate: bool = False,
    dry_run: bool = False,
) -> dict:
    """人工确认一组的标签:回写库 + 反哺别名表。

    返回 dict(session 已 commit):
      ok / reason / label / old_topic / messages / aliases_written
      / alias_skipped(为何没写别名)/ propagated_messages(连带改写的消息条数)
      / dry_run

    allow_new=False 时标签必须能收敛到标准集,否则返回 ok=False 且**不改库**
    (人工也可能手滑写错字,不能因为一次手滑就在库里造一个新标签)。
    propagate=True 时把库里其它「原标签」的组一并改成新标签——人工纠正一条
    之后,与之同错的存量组通常也该跟着改,否则只修了冰山一角。
    dry_run=True 时走完全相同的校验与计算,最后 rollback:预览与真跑的结论
    必须一致,否则预览没有意义。
    """
    cleaned = clean_label(label or "")
    if not cleaned:
        return {"ok": False, "reason": "标签为空", "label": ""}

    repo = MessageRepo(session)
    rows = repo.list_by_root(root_message_id, chat_id)
    if not rows:
        return {"ok": False, "reason": "组不存在或没有消息", "label": cleaned}

    if standards is None:
        from app.services.topic_service import get_standard_labels

        standards = get_standard_labels(session)
    standards_set = set(standards or [])

    # 人工输入也先收敛:手写「画面模糊」应落到标准名「画面模糊类问题」
    resolved = cleaned
    if standards_set and resolved not in standards_set:
        resolved = normalize_label(
            session,
            resolved,
            standards=standards,
            sim_threshold=_HUMAN_SIM_THRESHOLD,
        ) or cleaned
    if standards_set and resolved not in standards_set and not allow_new:
        return {
            "ok": False,
            "reason": f"{resolved!r} 不在标准标签集内(需 --allow-new 才接受)",
            "label": resolved,
            "standards_hint": sorted(standards_set)[:10],
        }

    old_topic = next((m.topic for m in rows if m.topic), "")
    n = repo.set_topic_for_group(chat_id, root_message_id, resolved, source=SOURCE_HUMAN)
    repo.set_topic_conf_for_group(chat_id, root_message_id, 1.0)
    ConversationRepo(session).set_topic(chat_id, root_message_id, resolved)

    # ---- 反哺别名表:只有当原标签是「系统自己造的、且不是标准标签」时才学 ----
    aliases_written: list[str] = []
    alias_skipped = ""
    if not old_topic or old_topic == resolved:
        alias_skipped = "原标签为空或与人工标签相同,无错可学"
    elif standards_set and old_topic in standards_set:
        # 硬护栏:标准标签之间不能建立别名,否则会连锁改写整个体系
        alias_skipped = "原标签是标准标签,建立别名会毒化标签体系"
    else:
        if _write_alias(session, old_topic, resolved):
            aliases_written.append(f"{old_topic} -> {resolved}")

    propagated = 0
    if propagate and old_topic and old_topic != resolved:
        # 本组已在上面的 set_topic_for_group 里改过,这里只会命中其它组
        propagated = _propagate_topic(session, old_topic, resolved, chat_id=None)

    if dry_run:
        session.rollback()  # 校验与计算都做过,但一行都不留
    else:
        session.commit()
    logger.info(
        "人工定标%s %s/%s: %r -> %r (别名 %s,连带 %d 条消息)",
        "(预览)" if dry_run else "",
        chat_id,
        root_message_id,
        old_topic,
        resolved,
        aliases_written or alias_skipped,
        propagated,
    )
    return {
        "ok": True,
        "reason": "",
        "label": resolved,
        "old_topic": old_topic,
        "messages": int(n),
        "aliases_written": aliases_written,
        "alias_skipped": alias_skipped,
        "propagated_messages": propagated,
        "dry_run": dry_run,
    }


def _write_alias(session: Session, alias: str, canonical: str) -> bool:
    """写一条人工别名(幂等)。已存在时:自动建议的可被人覆盖,人工维护的不动。"""
    if not alias or not canonical or alias == canonical:
        return False
    row = session.execute(
        select(TopicAlias).where(TopicAlias.alias == alias)
    ).scalar_one_or_none()
    if row is None:
        session.add(TopicAlias(alias=alias, canonical=canonical, auto=0))
        return True
    if row.canonical == canonical:
        return False
    if row.auto:
        # 自动建议的别名本来就在等人工裁决,人工确认的结果优先
        row.canonical = canonical
        row.auto = 0
        return True
    logger.warning(
        "别名冲突: %r 已人工维护为 %r,不覆盖为 %r", alias, row.canonical, canonical
    )
    return False


def _propagate_topic(
    session: Session, old: str, new: str, chat_id: str | None = None
) -> int:
    """把库里所有 topic == old 的消息改成 new(连带修正存量同错组)。

    不动 topic_source/conf:这些组是「按人工确认的映射推出来的」,既不是模型
    判的也不是人逐条看的,保持原样以免污染 gold set 统计。
    """
    from sqlalchemy import update

    from app.models.message import Message

    stmt = update(Message).where(Message.topic == old).values(topic=new)
    if chat_id:
        stmt = stmt.where(Message.chat_id == chat_id)
    changed = session.execute(stmt).rowcount or 0
    if changed:
        logger.info("别名回刷: %r -> %r 共 %d 条消息", old, new, changed)
        # conversations 是组的展示副本,同步一遍
        from app.models.conversation import Conversation

        cstmt = update(Conversation).where(Conversation.topic == old).values(topic=new)
        if chat_id:
            cstmt = cstmt.where(Conversation.chat_id == chat_id)
        session.execute(cstmt)
    return int(changed)


# ---------------------------------------------------------------- gold set
def gold_stats(session: Session, chat_id: str | None = None) -> dict:
    """gold set 规模:人工确认过的组数、标签分布、人工覆盖率。"""
    repo = MessageRepo(session)
    gold = repo.list_gold_roots(chat_id=chat_id, limit=10000)
    total_groups = _count_all_groups(session, chat_id)
    dist = Counter(g["label"] or "(空)" for g in gold)
    return {
        "gold_groups": len(gold),
        "total_groups": total_groups,
        "human_rate": round(len(gold) / total_groups, 4) if total_groups else 0.0,
        "labels": dict(dist.most_common(20)),
    }


def eval_gold(
    session: Session,
    chat_id: str | None = None,
    limit: int = 200,
    low_conf: float = DEFAULT_LOW_CONF,
) -> dict:
    """用当前判别器重跑 gold set,算 top-1 命中率(需 LLM,调 predict_group)。

    这是唯一能回答「改完召回/提示词到底有没有更准」的方法:gold set 是人工
    定过标的,模型预测与它比就是准确率。命中判定:预测标签 == 人工标签。
    """
    from app.services.topic_service import get_topic_service

    svc = get_topic_service()
    if not svc.enabled:
        return {"ok": False, "reason": "LLM 未配置,无法评估"}
    repo = MessageRepo(session)
    gold = repo.list_gold_roots(chat_id=chat_id, limit=limit)
    hit = miss = unknown = 0
    misses: list[dict] = []
    for g in gold:
        try:
            pred, conf = svc.predict_group(g["chat_id"], g["root_message_id"])
        except Exception as e:  # noqa: BLE001 - 单组失败不该中断整轮评估
            logger.warning("评估失败 %s/%s: %s", g["chat_id"], g["root_message_id"], e)
            continue
        if not pred:
            unknown += 1
        elif pred == g["label"]:
            hit += 1
        else:
            miss += 1
            misses.append(
                {
                    "chat_id": g["chat_id"],
                    "root_message_id": g["root_message_id"],
                    "gold": g["label"],
                    "pred": pred,
                    "conf": round(conf, 3),
                }
            )
    judged = hit + miss
    return {
        "ok": True,
        "total": len(gold),
        "hit": hit,
        "miss": miss,
        "unknown": unknown,
        "accuracy": round(hit / judged, 4) if judged else 0.0,
        "coverage": round(judged / len(gold), 4) if gold else 0.0,
        "low_conf_threshold": low_conf,
        "misses": misses[:50],
    }


def _count_all_groups(session: Session, chat_id: str | None) -> int:
    from sqlalchemy import func, select

    from app.models.message import Message

    stmt = select(Message.chat_id, Message.root_message_id).where(
        Message.root_message_id != ""
    )
    if chat_id:
        stmt = stmt.where(Message.chat_id == chat_id)
    sub = stmt.group_by(Message.chat_id, Message.root_message_id).subquery()
    return int(session.execute(select(func.count()).select_from(sub)).scalar() or 0)
