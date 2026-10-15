"""主题簇聚类服务（只读）—— 把复杂对话按主题归类整理。

对应设计文档 docs/TOPIC_CLUSTER_DESIGN.md 的 L1(降噪) / L2(时间硬切) / L4(聚类) 层落地；
「话题信息收录完整性」补强见 docs/TOPIC_COMPLETENESS.md。

核心思想：群聊消息在时间流里多线程交织、回复链断裂，光靠"切段"不够，
必须再加一层「跨段时间的语义聚合」，把被交织/断裂打散的同类讨论重新聚成一个个「主题簇」。

本实现默认【零 LLM 也能跑】：
  特征 = 中文内容字(单字) + 2-gram 加权（过滤停用字），
  聚类 = 消息级流式增量聚类，每个主题簇持有一个「指数衰减质心」，
  用 Dice 相似度让"交错返场"的同话题消息重新归位到同一簇，
  并配合「短回复就近接话」处理不带关键词的接话。
可选接入 LLM：增强主题措辞(L5b 概括) 与 模糊边界判定(同话题兜底)。

「话题信息收录完整」三层补强（保证一个话题的所有信息都进簇）：
  A. 显式回复链强并入 —— parent/root 已在某簇 ⇒ 强制并入(问答对/回复串不拆散)
  B. 噪声回应就近挂靠 —— "收到/好的/嗯"不再丢弃, 挂靠为 is_filler 参与节点
  C. 非文本证据收录   —— 图片/文件/视频挂靠为证据, file_name 进 evidence 列表

流程：
  L1 降噪：过滤"收到/好的/嗯"等无实义短消息、归一化图片/文件/链接/@
  L2 硬切：timeout(600s) 切出粗时间段，跨沉寂必为不同话题
  L4 聚类：段内消息逐个喂给「衰减质心」聚类，允许时间不连续的同话题归到同一簇(核心)
  L5 输出：主题簇列表（话题/条数/人数/代表消息/簇内消息/交错标记/回应数/证据文件）
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.dao.segment_repo import SegmentRepo
from app.services.llm import LLMClient
from app.services.segment_service import segment_by_time, finalize_stat

logger = logging.getLogger(__name__)

# 时间硬切阈值(秒),与方案Y一致
_DEFAULT_TIMEOUT_SEC = 600
# 质心 Dice 相似度阈值(单字+2gram 加权特征):
#   >= 高阈值  → 强相似, 直接并入该主题簇(允许时间不连续=跨段聚合)
_DICE_HIGH = 0.20
# 中等相似: 在「回看窗口」内活跃的簇才并入
_DICE_LOW = 0.12
# 质心指数衰减系数:每处理一条消息, 旧质心整体乘以此系数, 让簇"跟随"近期内容演化
# (避免老主题的字面特征长期滞留在质心, 稀释对新主题的判别)
_DECAY = 0.9
_DECAY_FLOOR = 0.05
# 中等相似时, 允许并入的"最近活跃"回看窗口(秒)——超过该窗口视为已沉寂, 不并入
_REENTRY_WINDOW_SEC = 1800
# 无内容相似但时间紧邻时, 短回复(<=字数)就近并入最近活跃簇(处理"收到/我遇到了"这类接话)
_SHORT_REPLY_MAX_CHARS = 6
# 噪声/非文本消息(收到/好的/图片/文件等)就近挂靠的时间窗(秒)——
# 落在此窗内则挂靠到"当前在聊的话题簇", 计入 message_count 但不算主题发言
_FILLER_WINDOW_SEC = 1200

# ---------------------------------------------------------------- L1 降噪
_NOISE_EXACT = {
    "收到", "好的", "可以", "知道", "嗯", "好", "ok", "Ok", "OK", "1", "行",
    "是的", "没问题", "了解", "谢谢", "收到收到", "好的收到", "好的好的",
    "收到哈", "好嘞", "哈哈哈", "哈哈", "嗯嗯", "哦", "嗯嗯嗯", "收到啦",
    "没问题收到", "okok", "Okk", "辛苦了", "谢谢啊", "感谢",
}


def _clean_text(text: str) -> str:
    """归一化:URL/@/图片/文件 等替换为占位token,去符号。"""
    if not text:
        return ""
    t = text
    t = re.sub(r"https?://\S+", " [链接] ", t)
    t = re.sub(r"@_?\w+|@[^\s]+", " [@名] ", t)
    t = re.sub(r"\[图片\]|\[image\]|图片", " [图片] ", t)
    t = re.sub(r"\[\]|\[文件|\[视频|\[语音", "  ", t)
    t = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9%]+", " ", t)  # 去标点/emoji,留中英文数字%
    return t.strip()


def _is_noise(text: str) -> bool:
    """是否无实义(不参与主题判定)。"""
    t = (text or "").strip().lower()
    if not t:
        return True
    if t in _NOISE_EXACT:
        return True
    normalized = re.sub(r"[\s❌✅👍💯😀🙏\U0001F300-\U0001FAFF]+", "", t)
    if not normalized:
        return True
    # 纯符号/纯数字/单字语气助词
    if re.fullmatch(r"[0-9]+", normalized):
        return True
    if normalized in {"嗯额", "哦哦", "啊", "嗯嗯", "收到谢谢"}:
        return True
    return False


def _featurize(rows) -> set[str]:
    """L4 特征化:取段内非噪声文本,生成 2-gram token 集合(含类型占位)。"""
    feats: set[str] = set()
    has_text = False
    for m in rows:
        raw = (m.text or "").strip()
        mtype = m.message_type or ""
        if mtype in ("image",):
            feats.add("[图片]")
            continue
        if mtype in ("media", "file"):
            feats.add(f"[{mtype}]")
            continue
        if _is_noise(raw):
            continue
        has_text = True
        for tok in _char_bigrams(_clean_text(raw)):
            feats.add(tok)
    if not has_text:
        # 全是图片/文件,用类型占位兜底,否则完全无法聚
        return feats or {"[杂项]"}
    return feats


def _char_bigrams(text: str) -> list[str]:
    """中文 2-gram(按处理后的文本连续两字符)。"""
    text = text.replace(" ", "")
    if len(text) <= 1:
        return [text] if text else []
    return [text[i : i + 2] for i in range(len(text) - 1)]


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ---------------------------------------------------------------- 权重质心特征
# 无信息量的单字符停用词(去除后只留"内容字", 提升主题判别信噪比)
_STOPCHARS = frozenset(
    "的了在是和不有也要我你他她它它们这那天么个上下就都还又再最把被让给对从"
    "到着像说知道请吗吧啊嗯哈呢嘛哦呀"
)


def _weights(rows) -> Counter:
    """加权特征表型:内容单字(权重1) + 连续2-gram(权重1.5) + 类型占位。

    相比纯 2-gram:
    - 加了单字, 使「投屏/投影/能投」等词素差异但同话题的消息仍共享字面特征, 召回更高;
    - 过滤停用字, 抑制「的了在是」等高频噪声字, 信噪比更高。
    """
    w: Counter = Counter()
    for m in rows:
        raw = (m.text or "").strip()
        mtype = m.message_type or ""
        if mtype == "image":
            w["[图片]"] += 1
            continue
        if mtype in ("media", "file"):
            w[f"[{mtype}]"] += 1
            continue
        if _is_noise(raw):
            continue
        prev = ""
        for ch in _clean_text(raw):
            if ch == " ":
                prev = ""
                continue
            if ch in _STOPCHARS:
                prev = ""
                continue
            w[ch] += 1
            if prev:
                w[prev + ch] += 1.5
            prev = ch
    return w


def _dice_counter(a: Counter, b: Counter) -> float:
    """两个权重 Counters 的 Dice 相似度: 2*|intersection| / (sum_a + sum_b)。"""
    na = sum(a.values())
    nb = sum(b.values())
    if na == 0 or nb == 0:
        return 0.0
    inter = sum((a & b).values())
    return 2.0 * inter / (na + nb)


def _decay_weights(w: Counter, factor: float = _DECAY, floor: float = _DECAY_FLOOR) -> Counter:
    """质心整体指数衰减, 抹去低于 floor 的噪声 token, 返回新 Counter。"""
    out: Counter = Counter()
    for k, v in w.items():
        nv = v * factor
        if nv >= floor:
            out[k] += nv
    return out


# ---------------------------------------------------------------- L4/L5 聚类
class _Cluster:
    """一个主题簇(流式增量构建)。

    关键设计:簇持有「衰减质心」weights(带权 2-gram + 类型占位)。
    - 每条并入的消息先对质心做指数衰减(让老主题 token 淡出), 再叠加新消息特征;
    - 由此质心"跟随"近期内容演化, 交错返场的话题(投屏又回来)仍能与本簇匹配,
      —— 这正是「跨段时间的语义聚合」(L4 核心)对交织话题的解法。
    - 簇还收录两类"参与节点", 保证话题信息完整(不改变质心):
        噪声回应(收到/好的) → is_filler=true, 计入 filler_count;
        非文本证据(图片/文件) → 记入 evidence 文件列表。
    """

    def __init__(self, weights: Counter, rows: Optional[list] = None, topic: str = ""):
        self.weights = Counter(weights)
        self.rows: list = list(rows or [])
        self.topic = topic or _topic_from_weights(self.weights)
        self.start_topic = self.topic  # 本簇起点话题(用于观察"从何聊起")
        self.last_sec: Optional[float] = None  # 簇内最近一条消息的时间(秒)
        self.filler_count = 0  # 噪声回应节点数(收到/好的等)
        self.evidence: list[dict] = []  # 非文本证据文件(图片/文件/视频)
        self._filler_ids: set[str] = set()  # 噪声回应节点的 message_id(供输出标记)

    def add(self, m, label: str = "") -> None:
        """并入一条"有主题内容"的消息(更新衰减质心 + 主题)。"""
        self.rows.append(m)
        sec = _to_sec(m.create_time)
        if sec is not None:
            self.last_sec = sec
        # 先衰减旧质心, 再叠新消息权重(新权重以其原始频次进入, 突显近期内容)
        self.weights = _decay_weights(self.weights)
        merged = self.weights + _weights([m])
        self.weights = Counter(merged)
        # 质心跟随近期内容演化, 主话题随之更新(保留 start_topic 观察话题漂移)
        self.topic = _topic_from_weights(self.weights) or self.topic

    def add_node(self, m, filler: bool = False) -> None:
        """并入一条"参与节点"(噪声回应/非文本证据), 不改动质心与主题。"""
        self.rows.append(m)
        sec = _to_sec(m.create_time)
        if sec is not None:
            self.last_sec = sec
        if filler:
            self.filler_count += 1
            if m.message_id:
                self._filler_ids.add(m.message_id)
        else:
            self._collect_evidence(m)

    def _collect_evidence(self, m) -> None:
        """从非文本消息提取证据文件信息, 存入 evidence 列表。"""
        mtype = m.message_type or ""
        if mtype in ("", "text"):
            return
        entry = {"type": mtype}
        raw = m.raw or {}
        fname = raw.get("file_name") or raw.get("name") or ""
        if fname:
            entry["file_name"] = fname
        if raw.get("mime_type"):
            entry["mime_type"] = raw["mime_type"]
        if raw.get("file_key"):
            entry["file_key"] = raw["file_key"]
        if entry not in self.evidence:
            self.evidence.append(entry)

    def reentry_ok(self, msg_sec: Optional[float], window: int = _REENTRY_WINDOW_SEC) -> bool:
        """该消息是否在簇的「回看窗口」内(中等相似时才要求)。"""
        if self.last_sec is None or msg_sec is None:
            return True
        return (msg_sec - self.last_sec) <= window


# 非文本消息类型(无正文, 不参与聚类特征, 作为证据挂靠)
_NON_TEXT_TYPES = ("image", "media", "file", "video", "audio")


def _is_nontext(m) -> bool:
    return (m.message_type or "") in _NON_TEXT_TYPES


def _topic_from_weights(w: Counter, max_len: int = 8) -> str:
    """按质心取简洁主题(2~8 字)。

    算法: 按权重降序选取 2-gram, 贪心跳过与已选字符重叠的项(避免滑窗 2-gram
    叠字成"投屏屏失失败"), 拼出干净主题; 没有 2-gram 时回退到 1 字。
    """
    if not w:
        return "非文本消息"
    placeholders = {"[图片]", "[media]", "[file]", "[杂项]", "[链接]", "[@名]"}
    grams = sorted(
        ((k, v) for k, v in w.items() if k not in placeholders),
        key=lambda kv: (-kv[1], kv[0]),
    )
    used: set[str] = set()
    label = ""
    for tok, _ in grams:
        if len(label) >= max_len:
            break
        if len(tok) == 2:
            c1, c2 = tok[0], tok[1]
            if c1 in used or c2 in used:
                continue  # 与已选词重叠, 跳过, 保持主题干净
            used.add(c1)
            used.add(c2)
            label += tok
        elif len(tok) == 1:
            if tok in used:
                continue
            used.add(tok)
            label += tok
    if not label:
        return "非文本消息"
    return label[:max_len]


def _to_sec(create_time: str) -> Optional[float]:
    """毫秒时间戳字符串 -> 绝对秒; 失败返回 None。"""
    try:
        ts = int(create_time)
        if ts > 10**12:
            ts /= 1000
        return float(ts)
    except (TypeError, ValueError):
        return None


def _segment_rows(rows, timeout_sec: int) -> list[list]:
    """把整群消息切成语义段(L2)。"""
    return segment_by_time(rows, timeout_sec)


def cluster_chat(
    chat_id: str,
    llm: Optional[LLMClient] = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> list[dict]:
    """对单个群执行主题聚类,返回主题簇列表(只读,不写库)。

    流程(L2 时间硬切 → L4 消息级增量聚类):
    1. 按 600s 硬切成粗粒度「时间段」——跨特别是必然的新话题;
    2. 段内不再整体并簇, 而是把每条非噪声消息逐个喂给「衰减质心」聚类,
       按 Dice 相似度并入既有主题簇或新建簇。交错(interleaved)话题:
       甲话题消息中间隔了乙话题, 只要甲的消息与甲的质心仍够相似
       (且若中等相似时簇未沉寂超过回看窗), 就会重新并入甲的簇。
    """
    with SessionLocal() as session:
        repo = SegmentRepo(session)
        rows = repo.list_messages_by_chat(chat_id)
    if not rows:
        return []

    # L2 硬切(粗时间段)
    segments = _segment_rows(rows, timeout_sec)
    clusters: list[_Cluster] = []
    msg_to_cluster: dict[str, int] = {}  # 消息 message_id -> 簇下标(供回复链强并入)

    def _new_cluster(w, m) -> int:
        """新建一个簇并把首条消息登记;返回簇下标。"""
        clusters.append(_Cluster(w, [m]))
        idx = len(clusters) - 1
        if m.message_id:
            msg_to_cluster[m.message_id] = idx
        return idx

    for seg in segments:
        for m in seg:
            mtype = m.message_type or ""
            raw_text = (m.text or "").strip()
            msg_sec = _to_sec(m.create_time)

            # 0) 显式回复链强并入: 被回复 / 回复链头 已在某簇 ⇒ 强同话题证据
            chain_idx = _reply_chain_idx(m, msg_to_cluster)
            if chain_idx is not None:
                filler = not raw_text or _is_noise(raw_text)
                clusters[chain_idx].add_node(m, filler=filler) if filler else clusters[chain_idx].add(m)
                if m.message_id:
                    msg_to_cluster[m.message_id] = chain_idx
                continue

            # 1) 非文本消息(图片/文件/视频) → 就近挂靠为证据节点(优先于噪声判定)
            if _is_nontext(m):
                recent = _most_recent(clusters)
                if recent is not None and recent.reentry_ok(msg_sec, _FILLER_WINDOW_SEC):
                    recent.add_node(m, filler=False)
                    if m.message_id:
                        msg_to_cluster[m.message_id] = clusters.index(recent)
                else:
                    # 无活跃簇可挂 → 自成一簇(保留这个话题的新起点证据)
                    _new_cluster(Counter({f"[{mtype}]": 1}), m)
                continue

            # 2) 噪声回应节点(收到/好的/嗯/纯emoji等) → 就近挂靠为 filler
            if _is_noise(raw_text):
                recent = _most_recent(clusters)
                if recent is not None and recent.reentry_ok(msg_sec, _FILLER_WINDOW_SEC):
                    recent.add_node(m, filler=True)
                    if m.message_id:
                        msg_to_cluster[m.message_id] = clusters.index(recent)
                # 时间上无归属的噪声 → 丢弃(没有簇可挂)
                continue

            # 3) 有主题内容的文本消息 → 正常内容聚类
            w = _weights([m])
            if not w:
                continue
            # L4 聚类: 找内容最相似的候选簇
            best_idx, best_score = -1, 0.0
            for i, c in enumerate(clusters):
                s = _dice_counter(w, c.weights)
                if s > best_score:
                    best_idx, best_score = i, s
            raw_len = len(raw_text)
            recent = _most_recent(clusters)  # 最近活跃簇(用于时间就近接话)

            if best_idx >= 0 and best_score >= _DICE_HIGH:
                # 强相似 → 并入(跨时间聚合, 交错返场话题归位)
                clusters[best_idx].add(m)
                msg_to_cluster[m.message_id] = best_idx
            elif best_idx >= 0 and best_score >= _DICE_LOW and llm is not None:
                # 中等相似 + LLM 兜底判定同话题
                same = _llm_same_topic(llm, _topic_from_weights(w), clusters[best_idx].topic)
                if same:
                    clusters[best_idx].add(m)
                    msg_to_cluster[m.message_id] = best_idx
                else:
                    _new_cluster(w, m)
            elif best_idx >= 0 and best_score >= _DICE_LOW:
                # 中等相似(无 LLM): 仅当簇在回看窗口内仍活跃才并入, 否则保守新建
                if clusters[best_idx].reentry_ok(msg_sec):
                    clusters[best_idx].add(m)
                    msg_to_cluster[m.message_id] = best_idx
                else:
                    _new_cluster(w, m)
            elif recent is not None and raw_len <= _SHORT_REPLY_MAX_CHARS and recent.reentry_ok(msg_sec, _REENTRY_WINDOW_SEC):
                # 短回复 + 无内容相似但时间紧邻最近活跃簇 → 就近接话
                # (处理"我遇到了/收到/试一下"这类不带关键词的接话)
                recent.add(m)
                msg_to_cluster[m.message_id] = clusters.index(recent)
            else:
                # 明显无关 / 有内容但偏离所有已有话题 → 新建主题簇
                # (允许与已有簇并存 = 时间上并排处理交错话题)
                _new_cluster(w, m)

    # L5b 可选: LLM 对每个簇给出更通顺的主题措辞(仅配 LLM 时)
    if llm is not None:
        for c in clusters:
            _rename_by_llm(llm, c)

    return [_to_dict(c) for c in clusters]


def _reply_chain_idx(m, msg_to_cluster: dict[str, int]):
    """显式回复链强并入: 若本消息正回复/回复链头已在某簇, 返回该簇下标; 否则 None。"""
    target = (m.parent_message_id or "").strip()
    root = (m.root_message_id or "").strip()
    # 优先按被回复消息定位; 退而按回复链根定位(独立消息 root=自身, 会命中自身新簇, 无妨)
    for key in (target, root):
        if key:
            idx = msg_to_cluster.get(key)
            if idx is not None:
                return idx
    return None


def _rename_by_llm(llm: LLMClient, c: _Cluster) -> None:
    """用 LLM 把簇的代表消息概括成一个通顺短主题;失败保留规则主题。"""
    try:
        lines = []
        for m in c.rows[:6]:
            t = (m.text or "").strip()
            if not t or _is_noise(t):
                t = f"({m.message_type or '非文本'})"
            lines.append(t[:40])
        if not lines:
            return
        prompt = "用2~8字中文概括下面这段群聊讨论的主题。只输出短语:\n" + "\n".join(lines)
        label = llm.chat("你是群聊主题概括器。", prompt)
        label = (label or "").strip().strip("\"'“”「」。 ")
        if label:
            c.topic = label[:12]
    except Exception:  # noqa: BLE001
        pass


def _most_recent(clusters: list[_Cluster]) -> Optional[_Cluster]:
    """返回最近活跃的簇(即加入时间最晚的簇); 无簇返回 None。"""
    best: Optional[_Cluster] = None
    for c in clusters:
        if best is None or (c.last_sec or 0) > (best.last_sec or 0):
            best = c
    return best


def _llm_same_topic(llm: LLMClient, a: str, b: str) -> bool:
    """LLM 判断两个主题是否同一话题。"""
    try:
        prompt = f"「{a}」和「{b}」是否在讨论同一个话题？只回答 是 或 否。"
        ans = llm.chat("你是主题一致性判定器。", prompt)
        return "是" in (ans or "")
    except Exception:  # noqa: BLE001
        return False


def _to_dict(c: _Cluster) -> dict:
    stats = finalize_stat(c.rows) if c.rows else {}
    content_count = len(c.rows) - c.filler_count
    return {
        "topic": c.topic,
        "start_topic": c.start_topic,
        "message_count": len(c.rows),
        "content_count": content_count,  # 有主题内容的消息数(含非文本证据)
        "filler_count": c.filler_count,  # 其中噪声回应节点数
        "evidence": c.evidence,          # 非文本证据文件列表
        "participants": sorted({m.sender_id for m in c.rows if m.sender_id}),
        "participant_count": stats.get("participant_count", 0),
        "start_ms": stats.get("start_ms", 0),
        "end_ms": stats.get("end_ms", 0),
        "interleaved": bool(stats.get("interleaved")),
        "repr": _cluster_repr(c.rows),
        "messages": [
            {
                "time": m.create_time or "",
                "sender": m.sender_id or "",
                "type": m.message_type or "",
                "text": (m.text or "").strip()[:120],
                "is_filler": bool(m.message_id and m.message_id in c._filler_ids),
            }
            for m in c.rows
        ],
    }


def _cluster_repr(rows: list) -> list[str]:
    """取代表消息文本(非噪声)至多 3 条。"""
    out: list[str] = []
    for m in rows:
        t = (m.text or "").strip()
        if _is_noise(t):
            continue
        out.append(t[:40])
        if len(out) >= 3:
            break
    return out


def _load_llm() -> Optional[LLMClient]:
    """按配置构建 LLM;未配置返回 None。"""
    settings = get_settings()
    if settings.llm_enabled:
        return LLMClient(
            settings.llm_api_base,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        )
    return None


def to_text(clusters: list[dict]) -> str:
    """渲染成控制台可读文本。"""
    if not clusters:
        return "（无消息可聚类）"
    # 按消息数降序
    clusters = sorted(clusters, key=lambda c: -c["message_count"])
    lines = [f"===== 主题归类整理 ({len(clusters)} 个主题簇) ====="]
    for i, c in enumerate(clusters, 1):
        extra = f"{c['message_count']}条/{c['participant_count']}人"
        if c.get("filler_count"):
            extra += f" (回应{c['filler_count']})"
        lines.append(f"\n[{i}] {c['topic']}  —  {extra}" + (" (交叉)" if c["interleaved"] else ""))
        if c.get("start_topic") and c["start_topic"] != c["topic"]:
            lines.append(f"    话题演化: {c['start_topic']} → {c['topic']}")
        if c.get("evidence"):
            fnames = [e.get("file_name") or e["type"] for e in c["evidence"]]
            lines.append("    证据文件: " + " ｜ ".join(fnames[:5]))
        if c["repr"]:
            lines.append("    代表: " + " ｜ ".join(c["repr"]))
    return "\n".join(lines)
