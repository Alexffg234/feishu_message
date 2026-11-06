"""消息数据访问层 (DAO)。

封装对 messages 表的读写,业务层不直接接触 SQLAlchemy 细节。
"""
from __future__ import annotations

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.message import Message


class MessageRepo:
    """Message 表的存取操作。"""

    def __init__(self, session: Session):
        self._session = session

    def add(self, msg: Message) -> Message:
        """把消息加入会话(仅 add 不提交),交由调用方统一提交。

        用于「消息 + 对话挂接」单事务场景:调用方在 _attach_conversation
        之后统一 commit,保证整笔写入的原子性。返回 msg 便于链式调用。
        """
        self._session.add(msg)
        return msg

    def insert(self, msg: Message) -> Message:
        """新增一条消息,返回对象(内部立即 commit,供独立写入场景使用)。"""
        self._session.add(msg)
        self._session.commit()
        self._session.refresh(msg)
        return msg

    def insert_many(self, msgs: list[Message]) -> None:
        """批量新增(用于回放/补拉)。"""
        if not msgs:
            return
        self._session.add_all(msgs)
        self._session.commit()

    def exists(self, chat_id: str, message_id: str) -> bool:
        stmt = select(Message.id).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
        return self._session.execute(stmt).first() is not None

    def count(self) -> int:
        stmt = select(Message.id)
        return len(self._session.execute(stmt).all())

    def list_recent(self, limit: int = 100) -> list[Message]:
        stmt = (
            select(Message)
            .order_by(Message.received_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def list_by_chat(self, chat_id: str, limit: int = 100) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.chat_id == chat_id)
            .order_by(Message.received_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def get_by_message_id(self, chat_id: str, message_id: str) -> Message | None:
        stmt = select(Message).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def update_raw(
        self, chat_id: str, message_id: str, raw: dict
    ) -> int:
        """按 (chat_id, message_id) 更新该消息的 raw(媒体 worker 回填 URL 用)。

        媒体异步 worker 解析出资源 URL 后,用它把已落库消息行的 raw 原地改写
        为带 URL 的版本。返回影响行数(0 = 消息不存在,幂等安全)。
        注意:需写入「新 dict 对象」,JSON 列才可被 SQLAlchemy 检测到变更。
        """
        from sqlalchemy import update

        stmt = (
            update(Message)
            .where(Message.chat_id == chat_id, Message.message_id == message_id)
            .values(raw=raw)
        )
        result = self._session.execute(stmt)
        self._session.commit()
        return result.rowcount or 0

    def list_roots(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[tuple[str, str]]:
        """最近 N 个分组,返回 [(root_message_id, chat_id), ...]。

        按每组内最新一条消息的时间倒序排列。
        """
        last_id = func.max(Message.id).label("last_id")
        stmt = (
            select(Message.root_message_id, Message.chat_id, last_id)
            .where(Message.root_message_id != "")
            .group_by(Message.root_message_id, Message.chat_id)
            .order_by(last_id.desc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        rows = self._session.execute(stmt).all()
        return [(r.root_message_id, r.chat_id) for r in rows]

    def list_by_root(
        self, root_message_id: str, chat_id: str | None = None
    ) -> list[Message]:
        """取同一分组(同一 root)下的全部消息,按时间正序。"""
        stmt = select(Message).where(Message.root_message_id == root_message_id)
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        stmt = stmt.order_by(Message.received_at.asc(), Message.id.asc())
        return list(self._session.scalars(stmt).all())

    # ---------------------------------------------------------------- topic
    def group_has_topic(self, chat_id: str, root_message_id: str) -> bool:
        stmt = (
            select(Message.id)
            .where(
                Message.chat_id == chat_id,
                Message.root_message_id == root_message_id,
                Message.topic != "",
            )
            .limit(1)
        )
        return self._session.execute(stmt).first() is not None

    def group_last_create_time(
        self, chat_id: str, root_message_id: str
    ) -> int | None:
        """组内最后一条消息的时间(毫秒 int);空组/时间脏数据返回 None。

        供入库侧护栏判断「这组是不是已经静默很久了」,不加载整组消息。
        """
        stmt = select(func.max(Message.create_time)).where(
            Message.chat_id == chat_id,
            Message.root_message_id == root_message_id,
        )
        val = self._session.execute(stmt).scalar()
        try:
            return int(val) if val else None
        except (TypeError, ValueError):
            return None

    def set_topic_for_group(
        self, chat_id: str, root_message_id: str, topic: str, source: str | None = None
    ) -> int:
        """整组(同一回复链)统一打 topic 标签,返回影响行数。

        source 传值时同写 topic_source("llm"/"human"),None 表示不动来源——
        LLM 重分类不该把人工确认过的标记洗掉,所以调用方按场景显式传。
        """
        from sqlalchemy import update

        values: dict = {"topic": topic}
        if source is not None:
            values["topic_source"] = source
        stmt = (
            update(Message)
            .where(
                Message.chat_id == chat_id,
                Message.root_message_id == root_message_id,
            )
            .values(**values)
        )
        return self._session.execute(stmt).rowcount or 0

    def set_stage_for_group(
        self, chat_id: str, root_message_id: str, stage: str
    ) -> int:
        """整组(同一回复链)统一写处理阶段,返回影响行数。

        与 topic 正交:topic 描述现象,stage 描述进展,两者各自独立更新。
        """
        from sqlalchemy import update

        stmt = (
            update(Message)
            .where(
                Message.chat_id == chat_id,
                Message.root_message_id == root_message_id,
            )
            .values(stage=stage)
        )
        return self._session.execute(stmt).rowcount or 0

    def set_topic_conf_for_group(
        self, chat_id: str, root_message_id: str, conf: float
    ) -> int:
        """整组写入归类置信度(0~1),供置信分流与人工复核排序使用。"""
        from sqlalchemy import update

        stmt = (
            update(Message)
            .where(
                Message.chat_id == chat_id,
                Message.root_message_id == root_message_id,
            )
            .values(topic_conf=conf)
        )
        return self._session.execute(stmt).rowcount or 0

    def get_existing_topics(self, limit: int = 100) -> list[str]:
        stmt = (
            select(Message.topic)
            .where(Message.topic != "")
            .distinct()
            .order_by(Message.topic)
            .limit(limit)
        )
        return [r[0] for r in self._session.execute(stmt).all()]

    def list_topics(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[tuple[str, int]]:
        """最近活跃的 N 个主题,返回 [(topic, 组内最新消息 id), ...]。"""
        last_id = func.max(Message.id).label("last_id")
        stmt = (
            select(Message.topic, last_id)
            .where(Message.topic != "")
            .group_by(Message.topic)
            .order_by(last_id.desc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        rows = self._session.execute(stmt).all()
        return [(r.topic, r.last_id) for r in rows]

    def list_by_topic(
        self, topic: str, chat_id: str | None = None, limit: int = 1000
    ) -> list[Message]:
        stmt = select(Message).where(Message.topic == topic)
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        stmt = stmt.order_by(Message.received_at.asc(), Message.id.asc()).limit(limit)
        return list(self._session.scalars(stmt).all())

    def count_unclassified_roots(self, chat_id: str | None = None) -> int:
        """未分类组总数(整组无任何非空 topic)。"""
        labeled = func.max(case((Message.topic != "", 1), else_=0)).label("labeled")
        sub = select(Message.chat_id, Message.root_message_id).where(
            Message.root_message_id != ""
        )
        if chat_id:
            sub = sub.where(Message.chat_id == chat_id)
        sub = sub.group_by(Message.chat_id, Message.root_message_id).having(
            labeled == 0
        ).subquery()
        return int(
            self._session.execute(select(func.count()).select_from(sub)).scalar() or 0
        )


    def list_unclassified_roots(
        self, chat_id: str | None = None, limit: int = 200
    ) -> list[dict]:
        """列出「整组都没有 topic」的回复链组(未分类清单)。

        注意 list_topics 只查 topic != '' 的行,看不到「没打上标签的组」——
        闭集判别上线后这批会静默堆积,故单列此查询。
        返回 [{"chat_id","root_message_id","message_count","first_message_id",
                "start_time"}],按组内最早消息 id 倒序(新的在前)。
        """
        labeled = func.max(case((Message.topic != "", 1), else_=0))
        first_id = func.min(Message.id).label("first_id")
        stmt = (
            select(
                Message.chat_id,
                Message.root_message_id,
                func.count(Message.id).label("n"),
                first_id,
                func.min(Message.create_time).label("start"),
            )
            .where(Message.root_message_id != "")
            .group_by(Message.chat_id, Message.root_message_id)
            .having(labeled == 0)
            .order_by(first_id.desc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        return [
            {
                "chat_id": r[0],
                "root_message_id": r[1],
                "message_count": int(r[2] or 0),
                "first_message_id": int(r[3] or 0),
                "start_time": r[4] or "",
            }
            for r in self._session.execute(stmt).all()
        ]

    def list_review_roots(
        self,
        chat_id: str | None = None,
        limit: int = 100,
        low_conf: float = 0.5,
        include_human: bool = False,
    ) -> list[dict]:
        """人工复核队列:未分类组 + 低置信组,未分类优先、组内越新越靠前。

        一个「组」= 一条回复链(chat_id + root_message_id)。入队条件取并集:
        - 未分类:整组无 topic(闭集拒掉的都在这儿,不盯就静默堆积);
        - 低置信:整组最大 topic_conf < low_conf(LLM 自己都没把握)。
        include_human=False 时排除人工已确认的组(topic_source='human'),
        否则人工审过的会被反复塞回队列。

        跨库可移植:不用 group_concat / || 拼接,只用 max/count/min 聚合。
        返回 [{"chat_id","root_message_id","topic","topic_conf","labeled",
                "message_count","last_time","first_message_id"}]。
        """
        labeled = func.max(case((Message.topic != "", 1), else_=0)).label("labeled")
        # topic 组内本应一致,max(空串被非空值取代)= 取到那个标签
        topic = func.max(Message.topic).label("topic")
        conf = func.max(Message.topic_conf).label("conf")
        human = func.max(case((Message.topic_source == "human", 1), else_=0)).label(
            "human"
        )
        first_id = func.min(Message.id).label("first_id")
        stmt = (
            select(
                Message.chat_id,
                Message.root_message_id,
                topic,
                conf,
                labeled,
                human,
                func.count(Message.id).label("n"),
                func.max(Message.create_time).label("last"),
                first_id,
            )
            .where(Message.root_message_id != "")
            .group_by(Message.chat_id, Message.root_message_id)
            .having(or_(labeled == 0, conf < low_conf))
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        if not include_human:
            stmt = stmt.having(human == 0)
        # 未分类优先(问题更大),再按最后活跃时间倒序(新的先处理)
        stmt = stmt.order_by(labeled.asc(), first_id.desc()).limit(limit)
        return [
            {
                "chat_id": r[0],
                "root_message_id": r[1],
                "topic": r[2] or "",
                "topic_conf": float(r[3] or 0.0),
                "labeled": int(r[4] or 0),
                "message_count": int(r[6] or 0),
                "last_time": r[7] or "",
                "first_message_id": int(r[8] or 0),
            }
            for r in self._session.execute(stmt).all()
        ]

    def count_review_roots(
        self,
        chat_id: str | None = None,
        low_conf: float = 0.5,
        include_human: bool = False,
    ) -> int:
        """复核队列总长度(不受 limit 截断)。"""
        labeled = func.max(case((Message.topic != "", 1), else_=0)).label("labeled")
        conf = func.max(Message.topic_conf).label("conf")
        human = func.max(case((Message.topic_source == "human", 1), else_=0)).label(
            "human"
        )
        sub = select(Message.chat_id, Message.root_message_id).where(
            Message.root_message_id != ""
        )
        if chat_id:
            sub = sub.where(Message.chat_id == chat_id)
        sub = sub.group_by(Message.chat_id, Message.root_message_id).having(
            or_(labeled == 0, conf < low_conf)
        )
        if not include_human:
            sub = sub.having(human == 0)
        sub = sub.subquery()
        return int(
            self._session.execute(select(func.count()).select_from(sub)).scalar() or 0
        )

    def get_group_samples(
        self, chat_id: str, roots: list[str], per_group: int = 2, max_len: int = 120
    ) -> dict[str, list[str]]:
        """一批组的样本文本(每组最早 per_group 条非空文本),供人工看内容判断。

        单次查询取回这批组的全部文本再在内存截断:组数受 limit 控制(默认 100),
        消息量可控;避免 group_concat(GROUP_CONCAT/string_agg 各家语法不同)。
        """
        if not roots:
            return {}
        stmt = (
            select(Message.root_message_id, Message.text)
            .where(
                Message.chat_id == chat_id,
                Message.root_message_id.in_(roots),
                Message.text != "",
            )
            .order_by(Message.id.asc())
        )
        out: dict[str, list[str]] = {}
        for root, text in self._session.execute(stmt):
            bucket = out.setdefault(root, [])
            if len(bucket) >= per_group:
                continue
            t = (text or "").strip()
            if t:
                bucket.append(t[:max_len])
        return out

    def list_gold_roots(
        self, chat_id: str | None = None, limit: int = 1000
    ) -> list[dict]:
        """人工确认过的组(gold set):评估判别准确率的唯一可信基线。

        返回 [{"chat_id","root_message_id","label","message_count"}]。
        """
        human = func.max(case((Message.topic_source == "human", 1), else_=0)).label(
            "human"
        )
        stmt = (
            select(
                Message.chat_id,
                Message.root_message_id,
                func.max(Message.topic).label("label"),
                func.count(Message.id).label("n"),
            )
            .where(Message.root_message_id != "")
            .group_by(Message.chat_id, Message.root_message_id)
            .having(human == 1)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        stmt = stmt.order_by(func.min(Message.id).desc()).limit(limit)
        return [
            {
                "chat_id": r[0],
                "root_message_id": r[1],
                "label": r[2] or "",
                "message_count": int(r[3] or 0),
            }
            for r in self._session.execute(stmt).all()
        ]

    def list_topic_roots(self, chat_id: str, topic: str) -> list[tuple[str, int]]:
        """同群同 topic 的所有分组,返回 [(root_message_id, first_message_id)]。

        按最早消息 id 升序(最旧组在前),供同主题归组决定归并方向。
        """
        first_id = func.min(Message.id).label("first_id")
        stmt = (
            select(Message.root_message_id, first_id)
            .where(
                Message.chat_id == chat_id,
                Message.topic == topic,
                Message.root_message_id != "",
            )
            .group_by(Message.root_message_id)
            .order_by(first_id.asc())
        )
        return [(r[0], int(r[1])) for r in self._session.execute(stmt).all()]

    def list_all_topic_groups(
        self, chat_id: str | None = None, limit: int = 100000
    ) -> list[tuple[str, str, str, int]]:
        """所有已打标签的组,返回 [(chat_id, topic, root, first_id)] 按最早消息升序。

        供归并回填脚本按 (chat, topic) 规划归并对。
        """
        first_id = func.min(Message.id).label("first_id")
        stmt = (
            select(Message.chat_id, Message.topic, Message.root_message_id, first_id)
            .where(Message.topic != "", Message.root_message_id != "")
            .group_by(Message.chat_id, Message.topic, Message.root_message_id)
            .order_by(first_id.asc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        return [
            (r[0], r[1], r[2], int(r[3]))
            for r in self._session.execute(stmt).all()
        ]

    def get_topic_samples(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[tuple[str, str]]:
        """每个已有主题取最新一条消息文本作摘要,返回 [(topic, text)]。

        分类提示词用它让 LLM 判断「是否同一问题」,优先同群(chat_id)。
        """
        stmt = (
            select(Message.topic, Message.text, Message.id)
            .where(Message.topic != "")
            .order_by(Message.id.desc())
            .limit(2000)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        samples: dict[str, str] = {}
        for topic, text, _mid in self._session.execute(stmt):
            if topic not in samples:
                samples[topic] = (text or "").strip()[:50]
            if len(samples) >= limit:
                break
        return list(samples.items())

    def get_label_exemplars(
        self, labels: list[str], per_label: int = 3, scan: int = 3000
    ) -> dict[str, list[str]]:
        """为给定标签各取最近 per_label 条消息文本,供候选召回打分。

        单次查询扫最近 scan 条已打标消息再在内存分桶(避免 41 个标签 41 次查询)。
        只用于召回排序:样例少只会让分数略钝,不影响正确性。
        """
        out: dict[str, list[str]] = {}
        if not labels or per_label <= 0:
            return out
        stmt = (
            select(Message.topic, Message.text)
            .where(Message.topic.in_(labels))
            .order_by(Message.id.desc())
            .limit(scan)
        )
        for topic, text in self._session.execute(stmt):
            bucket = out.setdefault(topic, [])
            if len(bucket) >= per_label:
                continue
            t = (text or "").strip()
            if t:
                bucket.append(t[:200])
        return out

    def list_recent_before(
        self, chat_id: str, before_id: int, limit: int = 6
    ) -> list:
        """同群内、id 小于 before_id 的最近 limit 条消息,按时间升序。

        主题分类时作上下文:让 LLM 判断当前组是否延续前面讨论。
        """
        stmt = (
            select(Message)
            .where(Message.chat_id == chat_id, Message.id < before_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        return list(reversed(self._session.execute(stmt).scalars().all()))

    def list_topic_openings(
        self, chat_id: str | None = None, limit: int = 100
    ) -> list[tuple[str, str]]:
        """每个主题取最早一条有信息量的消息作样本,返回 [(topic, text)]。

        归并判定时用案例开场消息比最新消息(常是「收到」类)更有代表性;
        过短文本(<6字)跳过。
        """
        stmt = (
            select(Message.topic, Message.text, Message.id)
            .where(Message.topic != "")
            .order_by(Message.id.asc())
            .limit(5000)
        )
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        samples: dict[str, str] = {}
        for topic, text, _mid in self._session.execute(stmt):
            t = (text or "").strip()
            if topic not in samples and len(t) >= 6:
                samples[topic] = t[:50]
            if len(samples) >= limit:
                break
        return list(samples.items())
