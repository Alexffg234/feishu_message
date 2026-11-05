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
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.models.topic_classify_lock import TopicClassifyLock
from app.services.group_merge import merge_topic_group
from app.services.label_norm import clean_label as _clean_label  # noqa: F401 (外部/测试引用)
from app.services.label_norm import _STOP_LABELS  # noqa: F401 (兼容引用)
from app.services.label_norm import normalize_label
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
# 在途租约最长持有时间(秒):超过即视为持有方崩溃,其他 worker 可接管重试
_CLAIM_LEASE_SECONDS = 300
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


# 纯语气词/确认词:不构成主题,当作空标签重试(定义见 label_norm._STOP_LABELS)


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
    def schedule_group(
        self, chat_id: str, root_message_id: str, force: bool = False
    ) -> bool:
        """为某回复链组调度异步主题分类(跨 worker 去重)。

        带去重:同组已在队列 / 已分类(done)/ 他人正在分类(在途租约)/
        失败冷却中均跳过;force=True 跳过全部去重强制重打。返回是否已调度。
        """
        if not self.enabled:
            return False
        key = (chat_id, root_message_id)
        # 进程内快速去重/冷却(单 worker 内第一道防线,兼供 wait_idle 使用)
        now = time.monotonic()
        with self._lock:
            if not force and key in self._pending:
                return False
            if not force and now - self._last_failure.get(key, 0.0) < _RETRY_COOLDOWN:
                return False
        # DB 级租约抢占(跨 worker 去重,防止两 worker 各调一次 LLM)
        if not self._try_claim(chat_id, root_message_id, force=force):
            return False
        with self._lock:
            self._pending.add(key)
        self._executor.submit(self._classify_worker, chat_id, root_message_id)
        return True

    def _try_claim(
        self, chat_id: str, root_message_id: str, force: bool = False
    ) -> bool:
        """以 DB 租约抢占本组的分类权,True=本进程中标、应执行分类。

        规则:
        - 组已带标签(历史数据无锁记录)→ 不重复分类(force 除外)
        - 锁记录 status='done'(已分类完成)→ 跳过(force 除外)
        - status='pending' 且租约在途/失败冷却中 → 跳过(他人正处理)
        - status='pending' 且租约过期(疑似崩溃)/冷却已过 → 接管重试
        - 无锁记录 → INSERT 抢占;唯一键冲突=并发下他人已抢先 → 跳过
        DB 异常时降级为允许(进程内去重仍兜底),不阻断入库调度。
        """
        if not force and self._group_has_topic(chat_id, root_message_id):
            return False
        now = datetime.now(timezone.utc)
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(TopicClassifyLock).where(
                        TopicClassifyLock.chat_id == chat_id,
                        TopicClassifyLock.root_message_id == root_message_id,
                    )
                ).scalar_one_or_none()
                if row is not None:
                    if not force and row.status == "done":
                        return False
                    if not force and not self._can_retake(row, now):
                        return False
                    # 接管(冷却已过 / 崩溃恢复 / force 重打)
                    row.status = "pending"
                    row.claimed_at = now
                    row.retry_at = None
                    session.commit()
                    return True
                # 无锁记录:INSERT 抢占;并发下唯一键冲突会被下方 except 捕获
                session.add(
                    TopicClassifyLock(
                        chat_id=chat_id,
                        root_message_id=root_message_id,
                        status="pending",
                        claimed_at=now,
                    )
                )
                session.commit()
                return True
        except IntegrityError:
            return False  # 并发下另一 worker 已抢先建锁,跳过
        except Exception:  # noqa: BLE001 - DB 不可用,降级为允许,进程内去重兜底
            logger.warning(
                "主题分类租约 DB 不可用,退化进程内去重 %s/%s", chat_id, root_message_id
            )
            return True

    def _can_retake(self, row: TopicClassifyLock, now: datetime) -> bool:
        """租约是否允许被当前 worker 接管(冷却已过或疑似崩溃)。"""
        if row.retry_at is not None:
            retry = row.retry_at
            if retry.tzinfo is None:  # SQLite 不存时区,裸时间按 UTC 解释
                retry = retry.replace(tzinfo=timezone.utc)
            return retry <= now
        claimed = row.claimed_at
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=timezone.utc)
        return (now - claimed).total_seconds() >= _CLAIM_LEASE_SECONDS

    def _group_has_topic(self, chat_id: str, root_message_id: str) -> bool:
        """该回复链组是否已带主题标签(已分类的历史组不重复调度)。"""
        try:
            with self._session_factory() as session:
                return MessageRepo(session).group_has_topic(chat_id, root_message_id)
        except Exception:  # noqa: BLE001 - DB 异常按未分类处理,由租约兜底
            return False

    def _mark_lock_done(self, chat_id: str, root_message_id: str) -> None:
        """分类成功后把租约标记为 done,幂等跳过后续重复调度。"""
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(TopicClassifyLock).where(
                        TopicClassifyLock.chat_id == chat_id,
                        TopicClassifyLock.root_message_id == root_message_id,
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.status = "done"
                    row.claimed_at = datetime.now(timezone.utc)
                    row.retry_at = None
                    session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("标记主题分类 done 失败 %s/%s", chat_id, root_message_id)

    def _mark_lock_retry(self, chat_id: str, root_message_id: str) -> None:
        """分类失败后进入冷却,期间其他 worker 不重复打同一组。"""
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(TopicClassifyLock).where(
                        TopicClassifyLock.chat_id == chat_id,
                        TopicClassifyLock.root_message_id == root_message_id,
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.status = "pending"
                    row.retry_at = datetime.now(timezone.utc) + timedelta(
                        seconds=_RETRY_COOLDOWN
                    )
                    session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("标记主题分类冷却失败 %s/%s", chat_id, root_message_id)

    def _classify_worker(self, chat_id: str, root_message_id: str) -> None:
        key = (chat_id, root_message_id)
        try:
            self.classify_group(chat_id, root_message_id)
            self._mark_lock_done(chat_id, root_message_id)
        except Exception:  # noqa: BLE001
            logger.exception("主题分类失败 %s/%s", chat_id, root_message_id)
            with self._lock:
                self._last_failure[key] = time.monotonic()
            self._mark_lock_retry(chat_id, root_message_id)
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
                current_text = " ".join(
                    (m.text or "").strip() for m in rows if (m.text or "").strip()
                )
                samples = self._existing_topic_samples(
                    repo, chat_id, current_text=current_text
                )
                standards = get_standard_labels(session)
                first_id = min(m.id for m in rows)
                context = repo.list_recent_before(chat_id, first_id, limit=6)
                label = self._llm_label(rows, samples, context, standards)
                if not label:
                    raise LLMError("LLM 返回空标签")
                # 标签归一化:别名收敛 -> 标准标签原样 -> (可选)相似度兜底。
                # 收敛后同义标签写入同一 canonical,归组(canonical 相等)才能归到一起。
                label = normalize_label(
                    session,
                    label,
                    standards=standards,
                    known_labels=[t for t, _s in samples],
                    sim_threshold=get_settings().topic_alias_sim_threshold,
                )
                if not label:
                    raise LLMError("LLM 标签归一化后为空")
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
        repo: MessageRepo,
        chat_id: str,
        current_text: str = "",
        limit: int = 20,
    ) -> list[tuple[str, str]]:
        """已有主题摘要(成本治理,阶段四 P1-B):优先同群,不足再用全局,去重。

        抽样顺序 = 与当前文本共现词优先 + 最近活跃兜底,上限收敛到 limit(默认 20),
        避免主题堆积时样本无限膨胀撑大 prompt、以及 LLM 因样本过多而「偷懒错沿用」。
        仍沿用「先同群后全局补位」语义;current_text 为当前待分类组的文本拼接。
        """
        # 候选池:多取一些(仍以最近活跃序)留出共现重排空间,避免直接截断丢失活跃主题
        pool_limit = min(max(limit * 3, limit + 12), 60)
        samples = repo.get_topic_samples(chat_id=chat_id, limit=pool_limit)
        if len(samples) < pool_limit:
            seen = {t for t, _s in samples}
            for topic, text in repo.get_topic_samples(limit=pool_limit):
                if topic not in seen:
                    samples.append((topic, text))
                    seen.add(topic)
                if len(samples) >= pool_limit:
                    break
        if not samples:
            return []
        if not current_text:
            return samples[:limit]
        return TopicService._rank_by_cooccurrence(samples, current_text, limit)

    @staticmethod
    def _rank_by_cooccurrence(
        samples: list[tuple[str, str]], current_text: str, limit: int
    ) -> list[tuple[str, str]]:
        """稳定排序:与当前文本共现字越多越靠前,其余保持最近活跃顺序。

        共现词(主题/摘要与当前文本共享的字符)往往提示「同类问题」,
        把它们提前送入 LLM,既提高沿用正确标签的概率,又不撑大样本量。
        """
        cur = set(current_text.replace(" ", ""))
        if not cur:
            return samples[:limit]

        def _key(item: tuple[str, str]) -> int:
            topic, text = item
            # 注意:set(x or "") & cur 的括号在 set 外,先构集合再做交集
            return -(
                len(set(topic or "") & cur) + len(set(text or "") & cur)
            )

        return sorted(samples, key=_key)[:limit]

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
    @staticmethod
    def _repr_rows(rows: list, limit: int = _MAX_ROWS_PROMPT) -> list:
        """代表消息抽样替代硬截断(阶段四 P1-B)。

        超过 limit 时不再简单截断前 N 条,而是优先保留高信息量消息:
        首条 + 末条 + 最长 3 条 + 有图片/文件/视频的消息(携带视觉/附件上下文),
        不足 limit 再按时间补中间消息覆盖全链。返回按消息 id 升序。
        """
        if len(rows) <= limit:
            return rows
        selected: list = []
        seen: set[int] = set()

        def add(m) -> None:
            if m.id not in seen:
                seen.add(m.id)
                selected.append(m)

        add(rows[0])
        add(rows[-1])
        # 最长优先(按文本长度降序)
        for m in sorted(rows, key=lambda x: len((x.text or "").strip()), reverse=True):
            if len(selected) >= limit:
                break
            add(m)
        # 图片/文件/视频消息元数据
        for m in rows:
            if len(selected) >= limit:
                break
            if m.message_type in ("image", "media", "file"):
                add(m)
        # 仍不足:按时间补中间消息
        if len(selected) < limit:
            for m in rows:
                if len(selected) >= limit:
                    break
                add(m)
        selected.sort(key=lambda x: x.id)
        return selected

    def _llm_label(
        self,
        rows: list,
        samples: list[tuple[str, str]],
        context: list | None = None,
        standards: list[str] | None = None,
    ) -> str:
        lines = []
        for m in self._repr_rows(rows):
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
