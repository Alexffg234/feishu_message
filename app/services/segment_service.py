"""话题段落切分服务。

方案Y:整群话题段——把群消息按时间流切成一段段「在聊同一件事」的片段。

切段信号(主判据 = 时间,辅助 = 发言者上下文,兜底 = LLM 语义):
1. 时间硬切:相邻间隔 > segment_timeout(默认600s) → 强制切开
2. LLM 语义粗并(可选):把相邻硬切段若干一批送 LLM,判断是否同一话题,同话题合并
   - 未配置 LLM / 调用失败 → 退化为纯时间规则,仍能得出合理段落
3. 标记:对最终段算 message_count / participant_count / interleaved(发言者交叉)

「人」是辅助上下文,不是合并依据:段内发言者来回切换只用于标记 interleaved,
不单独触发切段。

幂等:segment_chat 前会清理该群旧段,可反复跑。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.dao.segment_repo import SegmentRepo
from app.models.message import Message
from app.models.segment import MessageSegment
from app.services.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

# 时间硬切阈值(秒)。真实数据 P95≈703s:超过95%话题内间隔在12分钟内,
# 故取 600s(10分钟)作为默认硬切阈值,高于绝大多数话题内间隔、低于跨话题停顿。
_DEFAULT_TIMEOUT_SEC = 600
# 每批送给 LLM 判断的最大候选段数(控成本)
_LLM_BATCH_SIZE = 5
# 送给 LLM 判断的每段代表消息上限
_MAX_REPR_ROWS = 3

_SYSTEM_PROMPT = (
    "你是群聊话题分段判定器。下面按顺序给出若干相邻的消息段落(段内有时间、发言者和消息文本)。\n"
    "请判断这些段落是否在讨论同一个话题。\n"
    "规则:\n"
    "1. 话题相同的相邻段应合并(例如段1提问、段2继续回答同一问题)。\n"
    "2. 只输出需要合并的段序号对,格式如: 1,2 或 1,2,3(同一行内用逗号分隔的都是应合并的段)。\n"
    "3. 若全部都不该合并,只输出 无。\n"
    "4. 只输出序号,不要任何解释或标点之外的文字。"
)

# LLM 失败冷却(秒)
_RETRY_COOLDOWN = 60


def _to_sec(create_time: str) -> Optional[int]:
    """毫秒时间戳字符串 -> 绝对秒;失败返回 None。"""
    try:
        ts = int(create_time)
        if ts > 10**12:
            ts /= 1000
        return int(ts)
    except (TypeError, ValueError):
        return None


def parse_rrule_result(raw: str) -> set[int]:
    """解析 LLM 输出的应合并段组(1-based 序号集合)。"""
    out: set[int] = set()
    for tok in (raw or "").replace("，", ",").replace(" ", "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "无":
            continue
        try:
            out.add(int(tok))
        except ValueError:
            continue
    return out


def segment_by_time(rows: list[Message], timeout_sec: int = _DEFAULT_TIMEOUT_SEC) -> list[list[Message]]:
    """第1遍:按时间硬切,返回候选段(每段是消息列表,时间正序)。

    纯函数,不依赖 DB,便于测试。
    """
    if not rows:
        return []
    segments: list[list[Message]] = []
    cur: list[Message] = [rows[0]]
    prev = _to_sec(rows[0].create_time)
    for m in rows[1:]:
        cur_ts = _to_sec(m.create_time)
        if prev is not None and cur_ts is not None and (cur_ts - prev) > timeout_sec:
            segments.append(cur)
            cur = []
        cur.append(m)
        prev = cur_ts if cur_ts is not None else prev
    if cur:
        segments.append(cur)
    return segments


def finalize_stat(rows: list[Message]) -> dict:
    """第3遍:对一段消息算统计信息。"""
    if not rows:
        return {
            "start_ms": 0,
            "end_ms": 0,
            "root_message_id": "",
            "message_count": 0,
            "participant_count": 0,
            "interleaved": False,
        }
    starts = [_to_sec(m.create_time) for m in rows]
    real = [s for s in starts if s is not None]
    start_ms = (real[0] if real else 0) * 1000
    end_ms = (real[-1] if real else 0) * 1000
    root_id = (rows[0].message_id or "") if rows else ""
    participants = {m.sender_id for m in rows if m.sender_id}
    # 发言者交叉:相邻消息发言者不同次数超过消息数一半 → 认为有来回穿插
    switches = sum(
        1
        for a, b in zip(rows, rows[1:])
        if (a.sender_id or "") != (b.sender_id or "")
    )
    interleaved = switches >= max(1, len(rows) // 2)
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "root_message_id": root_id,
        "message_count": len(rows),
        "participant_count": len(participants),
        "interleaved": interleaved,
    }


class SegmentService:
    """切段服务。主入口 segment_chat(chat_id) 对单个群执行 硬切→粗并→写库。"""

    def __init__(
        self,
        session_factory=SessionLocal,
        llm: Optional[LLMClient] = None,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    ):
        self._session_factory = session_factory
        self._llm = llm
        self._timeout_sec = timeout_sec

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    def segment_chat(self, chat_id: str, dry_run: bool = False) -> dict:
        """对单个群执行重切,返回统计 dict。

        dry_run=True 时只算段、不写库(用于预览)。
        """
        with self._session_factory() as session:
            repo = SegmentRepo(session)
            rows = repo.list_messages_by_chat(chat_id)
            if not rows:
                return {"chat_id": chat_id, "messages": 0, "segments": 0, "skipped": True}

            # 第1遍:时间硬切
            candidates = segment_by_time(rows, self._timeout_sec)

            # 第2遍:LLM 粗并(可降级为纯规则)
            if self.enabled:
                candidates = self._merge_by_llm(candidates)

            # 若为 dry_run,不写库,返回段信息给调用方预览
            if dry_run:
                seg_info = []
                for seg_rows in candidates:
                    st = finalize_stat(seg_rows)
                    seg_info.append(
                        {
                            **st,
                            "repr": _segment_repr(seg_rows),
                        }
                    )
                return {
                    "chat_id": chat_id,
                    "messages": len(rows),
                    "segments": len(candidates),
                    "dry_run": True,
                    "segments_detail": seg_info,
                }

            # 幂等:清理旧段
            cleared = repo.clear_chat_segment(chat_id)

            # 写段
            seg_ids: dict[int, int] = {}  # 段内首条消息 Message.id -> segment_id
            seg_list: list[dict] = []
            for seg_rows in candidates:
                st = finalize_stat(seg_rows)
                seg = MessageSegment(
                    chat_id=chat_id,
                    topic="",
                    start_ms=st["start_ms"],
                    end_ms=st["end_ms"],
                    root_message_id=st["root_message_id"],
                    message_count=st["message_count"],
                    participant_count=st["participant_count"],
                    interleaved=1 if st["interleaved"] else 0,
                )
                repo.insert_segment(seg)
                msg_ids = [m.id for m in seg_rows if m.id]
                repo.set_segment_for_messages(seg.id, msg_ids)
                seg_list.append(
                    {
                        "segment_id": seg.id,
                        "start_ms": st["start_ms"],
                        "end_ms": st["end_ms"],
                        "message_count": st["message_count"],
                        "participant_count": st["participant_count"],
                        "interleaved": st["interleaved"],
                        "repr": _segment_repr(seg_rows),
                    }
                )
            session.commit()
            return {
                "chat_id": chat_id,
                "messages": len(rows),
                "segments": len(candidates),
                "cleared_old_segments": cleared,
                "segments_detail": seg_list,
            }

    def segment_all(self, dry_run: bool = False) -> list[dict]:
        """对全部有消息的群执行重切。"""
        with self._session_factory() as session:
            repo = SegmentRepo(session)
            chat_ids = repo.list_chat_ids()
        results = []
        for cid in chat_ids:
            try:
                r = self.segment_chat(cid, dry_run=dry_run)
            except Exception:  # noqa: BLE001
                logger.exception("切段失败 %s", cid)
                r = {"chat_id": cid, "error": "segment failed"}
            results.append(r)
        return results

    # ---------------------------------------------------------------- 内部
    def _merge_by_llm(self, segments: list[list[Message]]) -> list[list[Message]]:
        """第2遍:把相邻候选段分批送 LLM,判断是否同话题,同话题则合并。失败返回原样。

        采用非重叠窗口:每批取至多 _LLM_BATCH_SIZE 段,窗口按「原始段数」推进,
        避免合并缩短 batch 后重复处理已合并的段。
        """
        if len(segments) < 2:
            return segments
        merged: list[list[Message]] = []
        i = 0
        while i < len(segments):
            window = segments[i : i + _LLM_BATCH_SIZE]
            window_len = len(window)
            try:
                window = self._llm_merge_batch(window)
            except Exception:  # noqa: BLE001
                logger.warning("LLM 段合并失败,回退为纯时间分段")
            merged.extend(window)
            i += window_len  # 按原始段数推进窗口
        return merged

    def _llm_merge_batch(self, batch: list[list[Message]]) -> list[list[Message]]:
        """对一批候选段做一次 LLM 判断并就地合并,返回新段列表(顺序保持)。

        把 LLM 输出的"应合并序号组"(1-based)转为相邻段的合并。
        为简化且避免跨窗口的复杂依赖,这里只处理"连续的一段序号区间"的合并:
        例如 LLM 输出 {1,2,3} → 段1+2+3 合并;{1,2,4} → 只合并 1+2,4 单独。
        """
        if len(batch) < 2 or not self.enabled:
            return batch
        prompt_lines = []
        for idx, seg_rows in enumerate(batch, start=1):
            prompt_lines.append(f"段{idx}:{_segment_repr(seg_rows)}")
        user_prompt = "\n".join(prompt_lines)
        raw = self._llm.chat(_SYSTEM_PROMPT, user_prompt)
        merge_set = parse_rrule_result(raw)
        if not merge_set:
            return batch

        # 找到合并集里的连续区间,合并成一个段
        segs_set = sorted(merge_set)
        merged: list[list[Message]] = []
        used = set()
        j = 0
        while j < len(batch):
            if (j + 1) in segs_set:  # 1-based
                used.add(j + 1)
                # 收集从 j 开始连续需要合并的段
                run = [batch[j]]
                k = j + 1
                while k < len(batch) and (k + 1) in segs_set:
                    run.append(batch[k])
                    used.add(k + 1)
                    k += 1
                merged.append([m for part in run for m in part])
                j = k
            else:
                merged.append(batch[j])
                j += 1
        return merged


def _segment_repr(rows: list[Message]) -> str:
    """生成段落文本摘要(给 LLM 判断用):时间 + 首/末 + 最长消息 + 发言者数。"""
    if not rows:
        return "(空段)"
    stats = finalize_stat(rows)
    # 取有代表性的消息:首条 + 末条 + 最长文本
    reps = [rows[0]]
    if len(rows) > 1:
        reps.append(rows[-1])
    longest = max(rows, key=lambda m: len((m.text or "").strip()), default=rows[0])
    if longest.message_id not in {r.message_id for r in reps}:
        reps.append(longest)
    reps.sort(key=lambda m: _to_sec(m.create_time) or 0)
    lines = []
    for r in reps:
        txt = (r.text or "").strip() or f"({r.message_type or '非文本'})"
        lines.append(f"{txt[:50]}[{r.sender_id or '?'}]")
    return (
        f"{_fmt(rows[0].create_time)}~{_fmt(rows[-1].create_time)} "
        f"共{stats['message_count']}条/{stats['participant_count']}人 | "
        + " ；".join(lines)
    )


def _fmt(create_time: str) -> str:
    import datetime

    s = _to_sec(create_time)
    if s is None:
        return create_time or "-"
    return datetime.datetime.fromtimestamp(s).strftime("%H:%M")


@lru_cache
def get_segment_service(timeout_sec: int = _DEFAULT_TIMEOUT_SEC) -> SegmentService:
    """应用级单例。未配置 LLM 时返回纯时间规则实例。"""
    settings = get_settings()
    llm: Optional[LLMClient] = None
    if settings.llm_enabled:
        llm = LLMClient(
            settings.llm_api_base,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        )
    else:
        logger.info("LLM 段合并未配置(LLM_API_KEY/LLM_MODEL),使用纯时间规则切段")
    return SegmentService(llm=llm, timeout_sec=timeout_sec)
