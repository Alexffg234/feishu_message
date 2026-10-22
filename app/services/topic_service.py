"""LLM 主题分类服务。

- 入库不阻塞:消息落库后由后台线程池异步分类
- 以「回复链分组」为粒度打标签(1 组 = 1 次 LLM 调用),组内共享同一 topic
- 分类时把「标准标签」(问题大类配置的 topic 名)与库里已有主题列表
  (附最近消息摘要)传给 LLM:同类问题优先原样沿用标准标签,
  使新案例自动挂进 /summary 页对应大类
- 打完标签触发同主题归组:同群内相同 topic 的组归并到最早组
  (「直接发言无回复」的碎片反馈靠标签归回原问题块,不依赖时间窗)
  标准标签例外:多个案例共享同一标准标签,只归类不合并,
  同案例的后续会话由会话缝合(conv_stitch)负责
- 未配置 LLM / 调用失败时优雅降级:topic 留空,后续可重分类
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.services.group_merge import merge_topic_group
from app.services.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是群聊消息主题分类器。请把下面这段对话归纳为一个简短主题标签。\n"
    "要求:\n"
    "1. 优先沿用标准标签:「标准标签列表」是问题大类预配置的标准名称。"
    "若当前对话讨论的问题与其中某一项属于同一类问题(同类故障/同类事项,"
    "不要求同一台机器或同一用户),必须原样输出该标准标签,不得新造同义标签。\n"
    "2. 标签为 2~10 字中文短语(沿用标准标签时原样输出即可),概括对话讨论的功能或问题"
    "(例如:投屏、远场语音唤不醒、蓝牙连接)。\n"
    "3. 仅当能确认当前对话与某已有主题是同一台机器/同一故障案例的延续"
    "(如出现相同机器编号/UUID/MAC,或同一用户且前文明确在接续讨论该故障)"
    "时,才必须原样沿用对应的已有标签,不得新造同义标签。\n"
    "   注意:仅措辞或关键词相似(如都提到「推送」「闪退」「二维码」)但机器、用户、"
    "前文上下文与该主题无关联的,是全新问题,必须给新标签;\n"
    "   拿不准是否同一案例时,优先给新标签(宁可多一个对话,不可把新问题混进旧对话)。\n"
    "4. 只输出标签本身,不要引号、标点或任何解释。"
)

# 分类失败后,同一组的最小重试间隔(秒),防止 LLM 故障时反复打
_RETRY_COOLDOWN = 60
# 送给 LLM 的每组消息条数上限(控制 token)
_MAX_ROWS_PROMPT = 20
# 送给 LLM 的组前上下文消息条数(帮助判断是否延续前面讨论)
_MAX_CONTEXT = 6


def _fmt_time(create_time: str) -> str:
    """毫秒时间戳字符串 -> 可读时间;失败原样返回。"""
    try:
        ts = int(create_time) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return create_time or "-"


# 纯语气词/确认词:不构成主题,当作空标签重试
_STOP_LABELS = {
    "好的", "好", "收到", "嗯", "嗯嗯", "哦", "哦哦", "是", "是的", "对",
    "对对", "可以", "行", "ok", "ok的", "明白", "了解", "知道了",
    "谢谢", "好的谢谢", "好滴", "嗯好", "好嘞",
}


def get_standard_labels(session) -> list[str]:
    """问题大类配置的 topic 名(标准标签),按组展示顺序展开、去重。

    分类提示词注入该列表:同类问题沿用标准名,新案例自动挂进对应大类。
    """
    from app.models.topic_group import TopicGroup

    rows = (
        session.query(TopicGroup).order_by(TopicGroup.sort_order, TopicGroup.id).all()
    )
    labels: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for t in r.topics or []:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                labels.append(t)
    return labels


def _clean_label(raw: str) -> str:
    """清洗 LLM 输出:去引号/空白/首尾标点,截断到 20 字;语气词垃圾标签置空。"""
    label = (raw or "").strip().strip("\"'“”‘’「」 ")
    label = label.strip("。.!,!;；:：")
    if len(label) > 20:
        label = label[:20]
    if label.lower() in _STOP_LABELS:
        return ""
    return label


class TopicService:
    def __init__(
        self,
        session_factory=SessionLocal,
        llm: LLMClient | None = None,
    ):
        self._session_factory = session_factory
        self._llm = llm
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="topic")
        self._lock = threading.Lock()
        self._pending: set[tuple[str, str]] = set()
        self._last_failure: dict[tuple[str, str], float] = {}

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    # ---------------------------------------------------------------- 分类
    def schedule_group(self, chat_id: str, root_message_id: str) -> bool:
        """为某回复链组调度异步主题分类。

        带去重(同一组已在队列中则跳过)与失败冷却。返回是否已调度。
        """
        if not self.enabled:
            return False
        key = (chat_id, root_message_id)
        now = time.monotonic()
        with self._lock:
            if key in self._pending:
                return False
            failed_at = self._last_failure.get(key)
            if failed_at is not None and now - failed_at < _RETRY_COOLDOWN:
                return False
            self._pending.add(key)
        self._executor.submit(self._classify_worker, chat_id, root_message_id)
        return True

    def _classify_worker(self, chat_id: str, root_message_id: str) -> None:
        key = (chat_id, root_message_id)
        try:
            self.classify_group(chat_id, root_message_id)
        except Exception:  # noqa: BLE001
            logger.exception("主题分类失败 %s/%s", chat_id, root_message_id)
            with self._lock:
                self._last_failure[key] = time.monotonic()
        finally:
            with self._lock:
                self._pending.discard(key)

    def classify_group(
        self, chat_id: str, root_message_id: str, merge: bool = True
    ) -> str:
        """同步为一组消息打主题标签(亦供 --reclassify 使用)。

        已分类的组直接返回现有标签(merge=True 时仍尝试同主题归组)。
        merge=False 供回填脚本先只打标签不归并。失败抛 LLMError。
        """
        if not self.enabled:
            raise LLMError("LLM 未配置,无法分类")
        label = ""
        to_merge = ""
        with self._session_factory() as session:
            repo = MessageRepo(session)
            rows = repo.list_by_root(root_message_id, chat_id)
            if not rows:
                return ""
            existing_topic = next((m.topic for m in rows if m.topic), "")
            if not existing_topic and not any((m.text or "").strip() for m in rows):
                # 纯附件组(全为图片/视频/语音,无文本):LLM 无文本信号,
                # 不产「图片」这类泛标签,保持未标记独立成组
                return ""
            if existing_topic:
                # 分类后新到达的组内消息 topic 为空,统一回填(幂等 UPDATE)
                repo.set_topic_for_group(chat_id, root_message_id, existing_topic)
                ConversationRepo(session).set_topic(chat_id, root_message_id, existing_topic)
                session.commit()
                label = existing_topic
                to_merge = existing_topic if merge else ""
            else:
                samples = self._existing_topic_samples(repo, chat_id)
                standards = get_standard_labels(session)
                first_id = min(m.id for m in rows)
                context = repo.list_recent_before(chat_id, first_id, limit=6)
                label = self._llm_label(rows, samples, context, standards)
                if not label:
                    raise LLMError("LLM 返回空标签")
                repo.set_topic_for_group(chat_id, root_message_id, label)
                # conversations.topic 此前无人写入(页面读到恒为空),随分类同步
                ConversationRepo(session).set_topic(chat_id, root_message_id, label)
                session.commit()
                logger.info("主题分类 %s/%s -> %s", chat_id, root_message_id, label)
                # 标准标签=问题大类标准名,多个不同案例共享:只归类不合并,
                # 同案例的后续会话交给会话缝合(conv_stitch)处理
                to_merge = label if (merge and label not in standards) else ""
        # 归并用独立 session,避免与分类 session 并发占用连接
        if to_merge:
            self._merge_topic(chat_id, to_merge)
        return label

    def _merge_topic(self, chat_id: str, topic: str) -> None:
        """打完标签后触发同主题归组。归并失败只告警,不影响分类结果。"""
        try:
            with self._session_factory() as session:
                merged = merge_topic_group(session, chat_id, topic)
                if merged:
                    logger.info(
                        "同主题归组 %s/%s: %d 组并入最早组",
                        chat_id,
                        topic,
                        len(merged),
                    )
        except Exception:  # noqa: BLE001
            logger.exception("同主题归组失败(不影响分类) %s/%s", chat_id, topic)

    @staticmethod
    def _existing_topic_samples(
        repo: MessageRepo, chat_id: str, limit: int = 50
    ) -> list[tuple[str, str]]:
        """已有主题摘要:优先同群,不足再用全局,去重后截断。"""
        samples = repo.get_topic_samples(chat_id=chat_id, limit=limit)
        if len(samples) < limit:
            seen = {t for t, _s in samples}
            for topic, text in repo.get_topic_samples(limit=limit):
                if topic not in seen:
                    samples.append((topic, text))
                    seen.add(topic)
                if len(samples) >= limit:
                    break
        return samples

    def wait_idle(self, timeout: float = 30.0) -> None:
        """等待后台分类任务全部完成(测试用)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    return
            time.sleep(0.05)

    # ---------------------------------------------------------------- 查询
    def list_topics(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """按主题聚合最近活跃的话题,主题下嵌套回复链分组。

        每组: {topic, group_count, message_count, senders, groups:[MessageGroupOut 结构]}
        """
        with self._session_factory() as session:
            repo = MessageRepo(session)
            topics = repo.list_topics(chat_id=chat_id, limit=limit)
            result: list[dict] = []
            for topic, _last in topics:
                rows = repo.list_by_topic(topic, chat_id=chat_id)
                groups: dict[tuple[str, str], list] = {}
                for m in rows:
                    groups.setdefault((m.chat_id, m.root_message_id), []).append(m)

                group_outs: list[dict] = []
                senders: list[str] = []
                for (cid, root_id), grows in groups.items():
                    root_row = next((m for m in grows if m.message_id == root_id), None)
                    replies = [m for m in grows if m.message_id != root_id]
                    gsenders: list[str] = []
                    for m in grows:
                        if m.sender_id and m.sender_id not in gsenders:
                            gsenders.append(m.sender_id)
                        if m.sender_id and m.sender_id not in senders:
                            senders.append(m.sender_id)
                    group_outs.append(
                        {
                            "root_message_id": root_id,
                            "chat_id": cid,
                            "root": root_row,
                            "replies": replies,
                            "message_count": len(grows),
                            "senders": gsenders,
                        }
                    )

                def _latest(g: dict) -> int:
                    ids = [m.id for m in g["replies"]]
                    if g["root"] is not None:
                        ids.append(g["root"].id)
                    return max(ids) if ids else 0

                group_outs.sort(key=_latest, reverse=True)
                result.append(
                    {
                        "topic": topic,
                        "group_count": len(group_outs),
                        "message_count": len(rows),
                        "senders": senders,
                        "groups": group_outs,
                    }
                )
            return result

    # ---------------------------------------------------------------- 内部
    def _llm_label(
        self,
        rows: list,
        samples: list[tuple[str, str]],
        context: list | None = None,
        standards: list[str] | None = None,
    ) -> str:
        lines = []
        for m in rows[:_MAX_ROWS_PROMPT]:
            text = (m.text or "").strip() or f"({m.message_type or '非文本'})"
            lines.append(f"[{_fmt_time(m.create_time)}] {m.sender_id or '?'}: {text}")
        if samples:
            # 附最近消息摘要,便于 LLM 判断「是否同一问题」而沿用标签
            existing_str = "\n".join(
                f"- {topic}: {text or '(无摘要)'}" for topic, text in samples
            )
        else:
            existing_str = "(暂无)"
        ctx_lines = []
        for m in (context or [])[:_MAX_CONTEXT]:
            text = (m.text or "").strip()
            if not text:
                continue
            tag = f"(主题: {m.topic})" if m.topic else ""
            ctx_lines.append(
                f"[{_fmt_time(m.create_time)}] {m.sender_id or '?'}{tag}: {text[:60]}"
            )
        context_str = "\n".join(ctx_lines) if ctx_lines else "(无)"
        standards_str = "、".join(standards) if standards else "(无)"
        user_prompt = (
            f"标准标签(问题大类预配置的标准名称,同类问题必须原样沿用其一):\n{standards_str}\n\n"
            f"本组之前的最近消息(按时间升序,用于判断是否延续前面讨论):\n{context_str}\n\n"
            f"已有主题(附各主题最近一条消息摘要):\n{existing_str}\n\n"
            f"当前对话内容:\n" + "\n".join(lines)
        )
        raw = self._llm.chat(_SYSTEM_PROMPT, user_prompt)
        return _clean_label(raw)


@lru_cache
def get_topic_service() -> TopicService:
    """应用级单例。未配置 LLM 时返回 disabled 实例(查询仍可用)。"""
    settings = get_settings()
    llm: LLMClient | None = None
    if settings.llm_enabled:
        llm = LLMClient(
            settings.llm_api_base,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        )
    else:
        logger.info("LLM 主题分类未配置(LLM_API_KEY/LLM_MODEL),已禁用")
    return TopicService(llm=llm)
