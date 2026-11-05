"""标签归一化管线(话题归纳阶段一)。

把 LLM 自由生成的标签收敛到可治理的形态,供 topic_service 打标与归组使用:
1. 清洗: 去引号/标点/截断/语气词置空(迁自 topic_service._clean_label,语义一致)
2. 别名精确命中: topic_aliases.alias -> canonical(主机制,人工维护 + 自动建议)
3. 标准标签原样: 命中问题大类标准名时不改(标准名被多案例共享,不得再收敛)
4. 相似度兜底(可选): 与既有标签 2-gram Dice ≥ 阈值时收敛(默认关闭,避免误并)

依赖方向: label_norm -> cluster_service(复用 _char_bigrams/_dice_counter)。
不反向依赖 topic_service,故无循环导入。
"""
from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.message import Message
from app.models.topic_alias import TopicAlias

logger = logging.getLogger(__name__)

# 语气词/确认词:不构成主题,清洗时当作空标签
_STOP_LABELS = {
    "好的", "好", "收到", "嗯", "嗯嗯", "哦", "哦哦", "是", "是的", "对",
    "对对", "可以", "行", "ok", "ok的", "明白", "了解", "知道了",
    "谢谢", "好的谢谢", "好滴", "嗯好", "好嘞",
}

# 别名链解析深度上限 + 环保护
_ALIAS_MAX_HOPS = 5


def clean_label(raw: str) -> str:
    """清洗 LLM 输出:去引号/空白/首尾标点,截断到 20 字;语气词垃圾标签置空。"""
    label = (raw or "").strip().strip("\"'“”‘’「」 ")
    label = label.strip("。.!,!;；:：")
    if len(label) > 20:
        label = label[:20]
    if label.lower() in _STOP_LABELS:
        return ""
    return label


# 兼容旧引用:topic_service 曾以 _clean_label 命名并从顶部导出
_clean_label = clean_label


def _label_dice(a: str, b: str) -> float:
    """两个标签字符串的字符 2-gram Dice 相似度(0~1)。"""
    from app.services.cluster_service import _char_bigrams, _dice_counter

    wa = Counter(_char_bigrams(a.replace(" ", "")))
    wb = Counter(_char_bigrams(b.replace(" ", "")))
    if not wa or not wb:
        return 0.0
    return _dice_counter(wa, wb)


def _resolve_alias(session: Session, alias: str) -> str | None:
    """沿别名链解析到最终 canonical;无命中返回 None;防成环。"""
    cur = alias
    seen: set[str] = set()
    for _ in range(_ALIAS_MAX_HOPS):
        if cur in seen:
            return None  # 成环,放弃解析
        seen.add(cur)
        row = session.execute(
            select(TopicAlias.canonical).where(TopicAlias.alias == cur)
        ).scalar_one_or_none()
        if row is None:
            return cur if cur != alias else None
        cur = row
    return cur


def _existing_labels(session: Session, known_labels: list[str] | None) -> set[str]:
    """相似度兜底的候选标签集:优先用调用方传入,否则查库 distinct topic。"""
    out = set()
    if known_labels:
        out.update(t for t in known_labels if t)
    if out:
        return out
    try:
        rows = session.execute(
            select(Message.topic).where(Message.topic != "").distinct()
        ).scalars().all()
        out.update(t for t in rows if t)
    except Exception:  # noqa: BLE001 - 表缺失/连接异常时降级为空候选
        logger.warning("读取既有话题标签失败,相似度兜底候选为空")
    return out


def normalize_label(
    session: Session,
    raw: str,
    standards: list[str] | set[str] | None = None,
    known_labels: list[str] | None = None,
    sim_threshold: float = 0.0,
) -> str:
    """归一化一个标签:清洗 -> 别名收敛 -> 标准标签原样 -> (可选)相似度兜底。

    返回收敛后的标签;清洗后为空(语气词)时返回空串。
    standards: 问题大类标准名集合,命中时原样返回,不作相似度收敛。
    known_labels: 相似度兜底候选(复用已取到的样本 topic,避免重复查库)。
    sim_threshold: >0 时开放 2-gram Dice 兜底;=0 禁用(默认安全)。
    """
    cleaned = clean_label(raw)
    if not cleaned:
        return ""

    standards_set = set(standards) if standards else set()

    # 1) 别名精确命中(主机制)
    resolved = _resolve_alias(session, cleaned)
    if resolved:
        return resolved

    # 2) 标准标签原样:避免把标准名再收敛到别的相似标签
    if cleaned in standards_set:
        return cleaned

    # 3) 相似度兜底(可选,默认关闭)
    if sim_threshold > 0:
        best, best_score = "", 0.0
        for cand in _existing_labels(session, known_labels) | standards_set:
            if not cand or cand == cleaned:
                continue
            score = _label_dice(cleaned, cand)
            if score > best_score:
                best, best_score = cand, score
        if best and best_score >= sim_threshold:
            logger.info("标签相似度兜底 %r -> %r (dice=%.2f)", cleaned, best, best_score)
            return best

    return cleaned
