"""标准标签候选召回:把「41 选 1」缩成「8 选 1」。

闭集准不准,一半取决于候选集大小。标签一多,模型容易在近义标签之间摇摆
(「画面模糊」/「画面模糊类问题」/「画质不清晰」),提示词也长。做法是先纯字面
召回 top_k 个候选,再让 LLM 在候选里做选择题。

召回必然有漏风险,所以**只窄化、不放宽**:
- 候选永远是标准集的子集,下游闭集校验照旧生效;
- 窄化后 LLM 判 UNKNOWN 时,调用方用全集再问一次(只在失败路径多一次调用);
- 没有任何字面信号时**不做窄化**——瞎猜式的窄化比不窄化更糟。

打分(零 LLM 成本,三路取最大):
1. 文本直接包含标签名 → 满分(最强信号);
2. 覆盖度 |标签2-gram ∩ 文本| / |标签2-gram|:短标签对长文本时比 Dice 稳
   (Dice 的分母含文本长度,长文本会把分数稀释到接近 0);
3. 该标签历史消息与当前文本的 Dice × 0.9:标签名不含字面时唯一起作用的一路
   (「一闪一闪的」能召回「屏幕闪烁」靠的就是历史样例)。

注意 2 和 3 都封顶 0.99,保证「精确命中」严格排在最前,顺序确定可测。

**已知局限**:候选可能集中在同一大类(「画面糊了」会把 7 个含「画面」的标签
全部召回,其它大类一个都进不来)。这不是 bug——此时正确答案就在那一族里,
候选给了模型最好的对照;真答案在别的大类时,模型会判 UNKNOWN,由调用方拿
全集重问一次兜底。三条路合起来的漏检率看 `get_stats()["fallback_rate"]`。
"""
from __future__ import annotations

import logging
from collections import Counter

from app.services.cluster_service import _char_bigrams, _dice_counter

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8
# 每个标准标签最多取几条历史消息做样例(取多了召回更准,但只影响排序)
EXEMPLARS_PER_LABEL = 3
_EXEMPLAR_WEIGHT = 0.9
_FUZZY_CAP = 0.99

# 进程内累计(与 label_norm.get_norm_stats 同口径:重启归零,用于观察趋势)
_stats = {"calls": 0, "narrowed": 0, "fallback": 0}


def _dice(a: str, b: str) -> float:
    """两段文本的字符 2-gram Dice 相似度(0~1)。"""
    wa = Counter(_char_bigrams((a or "").replace(" ", "")))
    wb = Counter(_char_bigrams((b or "").replace(" ", "")))
    if not wa or not wb:
        return 0.0
    return _dice_counter(wa, wb)


# 标签尾部的「类别后缀」不承载现象信息,但会稀释覆盖度:「画面模糊类问题」
# 7 个字里 3 个是「类问题」,不剥掉的话文本全中前 4 字也只得 2/6=0.33。
_LABEL_SUFFIXES = ("类问题", "的问题", "的情况", "类故障", "类")


def _core_label(label: str) -> str:
    """剥掉尾部类别后缀后的标签主体(剥完过短则原样返回)。"""
    out = (label or "").replace(" ", "")
    for suf in _LABEL_SUFFIXES:
        if out.endswith(suf) and len(out) > len(suf) + 1:
            return out[: -len(suf)]
    return out


def _cover(label: str, text: str) -> float:
    """标签被文本覆盖的比例:标签的 2-gram 有多少比例出现在文本里。

    「画面模糊」(画面/面模/模糊)出现在「电视画面很模糊」里 → 2/3;
    用 Dice 算同一对只有 ~0.2,长文本下几乎不可用。
    原标签与剥后缀后的主体各算一次取最大(剥完只会更高,不会更低)。
    """
    pool = set(_char_bigrams((text or "").replace(" ", "")))
    if not pool:
        return 0.0
    best = 0.0
    for variant in {label or "", _core_label(label)}:
        grams = set(_char_bigrams(variant.replace(" ", "")))
        if not grams:
            continue
        best = max(best, sum(1 for g in grams if g in pool) / len(grams))
    return best


def score_label(label: str, text: str, examples=()) -> float:
    """单个标签与当前文本的相关度(0~1)。examples 为该标签的历史消息文本。"""
    flat_label = (label or "").replace(" ", "")
    flat_text = (text or "").replace(" ", "")
    if not flat_label or not flat_text:
        return 0.0
    if flat_label in flat_text:
        return 1.0
    best = _cover(label, text)
    for ex in examples or ():
        best = max(best, _dice(ex, text) * _EXEMPLAR_WEIGHT)
    return min(best, _FUZZY_CAP)


def recall_standards(
    current_text: str,
    standards: list[str],
    exemplars: dict[str, list[str]] | None = None,
    top_k: int | None = None,
) -> list[str]:
    """召回候选标签(按相关度降序),返回标准集的子集。

    以下情形**原样返回全集**(窄化无意义或有害):
    - 标准集本身不超过 top_k;
    - top_k <= 0(关闭召回);
    - 所有标签得分都是 0(没有任何字面信号,窄化等于瞎猜)。
    """
    if not standards:
        return []
    k = DEFAULT_TOP_K if top_k is None else top_k
    if k <= 0 or len(standards) <= k:
        return list(standards)
    exemplars = exemplars or {}
    scored = [
        (score_label(label, current_text, exemplars.get(label)), idx, label)
        for idx, label in enumerate(standards)
    ]
    if max(s for s, _i, _l in scored) <= 0.0:
        return list(standards)
    scored.sort(key=lambda x: (-x[0], x[1]))  # 同分保持标准集原顺序
    return [label for _s, _i, label in scored[:k]]


def filter_tree(tree: list[dict], keep: set[str]) -> list[dict]:
    """把大类树裁到候选标签:整类无候选的大类节点直接摘掉。

    保留「大类 > 子类」层级——大类名本身是语境(「画面类 > 花屏」比裸标签
    「花屏」好判),所以只裁叶子标签、不把节点拍平。
    """
    out: list[dict] = []
    for node in tree or []:
        item: dict = {"name": node.get("name", "")}
        topics = [t for t in (node.get("topics") or []) if t in keep]
        if topics:
            item["topics"] = topics
        subs = []
        for sub in node.get("sub") or []:
            sub_topics = [t for t in (sub.get("topics") or []) if t in keep]
            if sub_topics:
                subs.append({"name": sub.get("name", ""), "topics": sub_topics})
        if subs:
            item["sub"] = subs
        if item.get("topics") or item.get("sub"):
            out.append(item)
    return out


# ---------------------------------------------------------------- 进程内计数
def note_call(narrowed: bool) -> None:
    _stats["calls"] += 1
    if narrowed:
        _stats["narrowed"] += 1


def note_fallback() -> None:
    """窄化后 LLM 判 UNKNOWN、用全集重问的次数(召回漏检率)。"""
    _stats["fallback"] += 1


def get_stats() -> dict:
    out = dict(_stats)
    out["narrow_rate"] = round(out["narrowed"] / out["calls"], 4) if out["calls"] else 0.0
    out["fallback_rate"] = (
        round(out["fallback"] / out["narrowed"], 4) if out["narrowed"] else 0.0
    )
    return out


def reset_stats() -> None:
    for key in _stats:
        _stats[key] = 0
