"""主题 AI 总结服务集成测试(内存 SQLite + 假 LLM)。

验证:JSON 四字段生成落库、指纹未变走缓存不调 LLM、指纹变化重生成、
JSON 解析失败降级、有效文本不足跳过、DB 租约互斥、租约释放、
/api/topics 统计聚合、纯读缓存接口。
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, SchedulerLock
from app.models.topic_summary import TopicSummary
from app.schemas.message import MessageCreate
from app.services.message_service import MessageService
from app.services.topic_summary_service import (
    TopicSummaryService,
    _parse_llm_json,
)
import app.services.user_name_service as uns

T0 = 1700000000000
CHAT = "oc_summary"

JSON_ANSWER = json.dumps(
    {
        "problem": "电视音爆声",
        "investigation": "1. 检查音量\n2. 换源测试",
        "resolution": "升级固件",
        "status": "排查中",
    },
    ensure_ascii=False,
)


class FakeLLM:
    def __init__(self, answer: str = JSON_ANSWER):
        self.answer = answer
        self.calls: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.answer


class _NoopNameService:
    def resolve_many(self, user_ids):
        return {}


@pytest.fixture(autouse=True)
def _no_feishu_names(monkeypatch):
    """上下文构建时跳过飞书姓名反查(测试环境无凭据,避免打真实 DB/飞书)。"""
    monkeypatch.setattr(uns, "get_user_name_service", lambda: _NoopNameService())
    yield


def _setup():
    # StaticPool:所有 session 共享同一连接(内存 SQLite 每连接独立建库,
    # 共享连接才能跨 session 看到同一批表)
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session, Session


def _msg(mid, sender, ts, parent="", root="", text=None, mtype="text"):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type=mtype,
        text=text if text is not None else f"msg-{mid}",
        create_time=str(ts),
        parent_message_id=parent,
        root_message_id=root,
    )


def _set_topic(session, root, topic):
    from app.dao.conversation_repo import ConversationRepo
    from app.dao.message_repo import MessageRepo

    MessageRepo(session).set_topic_for_group(CHAT, root, topic)
    ConversationRepo(session).set_topic(CHAT, root, topic)
    session.commit()


def _seed(svc, session):
    """一个 topic 下两个对话,各 3 条文本。"""
    svc.save_message(_msg("m1", "u1", T0, text="电视看久了有音爆声"))
    svc.save_message(_msg("m1b", "u2", T0 + 30_000, parent="m1", text="我家也是呢"))
    svc.save_message(_msg("m1c", "u1", T0 + 60_000, parent="m1", text="已经换了音量模式"))
    svc.save_message(_msg("m2", "u3", T0 + 120_000, text="另一台也有音爆"))
    svc.save_message(_msg("m2b", "u4", T0 + 150_000, parent="m2", text="开 HDMI 时明显"))
    svc.save_message(_msg("m2c", "u3", T0 + 180_000, parent="m2", text="已反馈"))
    _set_topic(session, "m1", "音爆")
    _set_topic(session, "m2", "音爆")
    session.commit()


def _svc(Session, llm):
    return TopicSummaryService(session_factory=Session, llm=llm)


def test_generate_topic_json_fields():
    svc, session, Session = _setup()
    _seed(svc, session)
    ts = _svc(Session, FakeLLM())
    out = ts.generate_topic(CHAT, "音爆")
    assert out is not None
    assert out["generated_now"] is True
    assert out["problem"] == "电视音爆声"
    assert out["investigation"] == "1. 检查音量\n2. 换源测试"
    assert out["resolution"] == "升级固件"
    assert out["status"] == "排查中"
    row = session.query(TopicSummary).one()
    assert row.chat_id == CHAT and row.topic == "音爆"
    assert row.message_count == 6
    assert len(ts._llm.calls) == 1
    assert "案例 1" in ts._llm.calls[0] and "案例 2" in ts._llm.calls[0]
    print("generate json OK")


def test_fingerprint_fresh_cache_no_llm():
    svc, session, Session = _setup()
    _seed(svc, session)
    llm = FakeLLM()
    ts = _svc(Session, llm)
    ts.generate_topic(CHAT, "音爆")
    out = ts.generate_topic(CHAT, "音爆")
    assert out["generated_now"] is False
    assert len(llm.calls) == 1, "指纹未变不应再调 LLM"
    print("fresh cache OK")


def test_fingerprint_stale_regenerates():
    svc, session, Session = _setup()
    _seed(svc, session)
    llm = FakeLLM()
    ts = _svc(Session, llm)
    ts.generate_topic(CHAT, "音爆")
    # 新消息进入该 topic → 指纹变化
    svc.save_message(
        MessageCreate(
            message_id="m3",
            chat_id=CHAT,
            sender_id="u5",
            sender_type="user",
            message_type="text",
            text="又复现了",
            create_time=str(T0 + 300_000),
        )
    )
    _set_topic(session, "m3", "音爆")
    out = ts.generate_topic(CHAT, "音爆")
    assert out["generated_now"] is True
    assert len(llm.calls) == 2
    row = session.query(TopicSummary).one()
    assert row.message_count == 7
    print("stale regenerate OK")


def test_parse_failure_fallback():
    svc, session, Session = _setup()
    _seed(svc, session)
    ts = _svc(Session, FakeLLM("这是一段没有 JSON 的回答"))
    out = ts.generate_topic(CHAT, "音爆")
    assert out is not None
    assert out["problem"] == "这是一段没有 JSON 的回答"
    assert out["status"] == "未知"
    print("parse fallback OK")


def test_status_out_of_enum():
    assert (
        _parse_llm_json('{"problem": "x", "status": "乱写"}')["status"] == "乱写"
    )
    svc, session, Session = _setup()
    _seed(svc, session)
    ts = _svc(
        Session,
        FakeLLM(json.dumps({"problem": "x", "status": "乱写"}, ensure_ascii=False)),
    )
    out = ts.generate_topic(CHAT, "音爆")
    assert out["status"] == "未知"
    print("status enum OK")


def test_insufficient_text_skip():
    svc, session, Session = _setup()
    # 1 条文本 + 2 张图片 = 有效文本不足
    svc.save_message(_msg("p1", "u1", T0, mtype="image", text=""))
    svc.save_message(_msg("p2", "u1", T0 + 10_000, text="看下这个图"))
    svc.save_message(_msg("p3", "u1", T0 + 20_000, mtype="image", text=""))
    _set_topic(session, "p1", "看图")
    _set_topic(session, "p2", "看图")
    llm = FakeLLM()
    ts = _svc(Session, llm)
    assert ts.generate_topic(CHAT, "看图") is None
    assert llm.calls == [], "有效文本不足不应调 LLM"
    assert session.query(TopicSummary).count() == 0
    print("insufficient text skip OK")


def test_run_round_lock_and_release():
    svc, session, Session = _setup()
    _seed(svc, session)
    llm = FakeLLM()
    ts = _svc(Session, llm)
    # 模拟其他 worker 持锁(未过期)
    session.add(
        SchedulerLock(
            name="topic_summary_round",
            token="other-worker",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    session.commit()
    assert ts.run_round() == 0
    assert llm.calls == [], "租约被占用时不应生成"
    assert session.query(SchedulerLock).count() == 1, "他人租约不应被删除"

    # 租约过期后可接管,跑完应释放
    session.query(SchedulerLock).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    session.commit()
    assert ts.run_round() == 1
    assert len(llm.calls) == 1
    assert session.query(SchedulerLock).count() == 0, "自己跑完应释放租约"
    print("lock & release OK")


def test_run_round_idempotent():
    svc, session, Session = _setup()
    _seed(svc, session)
    llm = FakeLLM()
    ts = _svc(Session, llm)
    assert ts.run_round() == 1
    assert ts.run_round() == 0, "指纹未变第二轮不应再生成"
    assert len(llm.calls) == 1
    print("round idempotent OK")


def test_get_cached_missing_and_fresh():
    svc, session, Session = _setup()
    _seed(svc, session)
    ts = _svc(Session, FakeLLM())
    missing = ts.get_cached(CHAT, "音爆")
    assert missing["found"] is False and missing["enabled"] is True
    ts.generate_topic(CHAT, "音爆")
    fresh = ts.get_cached(CHAT, "音爆")
    assert fresh["found"] is True and fresh["stale"] is False
    print("get cached OK")


def test_api_topics_stats():
    from app.api.routes import api_topics

    svc, session, Session = _setup()
    _seed(svc, session)
    # 一个未分类的独立消息
    svc.save_message(_msg("u1", "u9", T0 + 500_000, text="随便说点别的"))
    out = api_topics(limit=50, db=session)
    assert out["overall"]["topic_count"] == 1
    assert out["overall"]["message_count"] == 7
    assert out["overall"]["conversation_count"] == 3
    assert out["overall"]["unclassified_conversations"] == 1
    t = out["topics"][0]
    assert t["topic"] == "音爆"
    assert t["conversation_count"] == 2
    assert t["message_count"] == 6
    assert t["participant_count"] == 4
    assert t["summary_status"] == "missing"
    assert t["conversation_ids"] == sorted(t["conversation_ids"]) and len(t["conversation_ids"]) == 2
    print("api topics stats OK")


def test_api_topic_summary_read_and_force(monkeypatch):
    from fastapi import HTTPException

    from app.api.routes import api_topic_summary, api_topic_summary_regenerate
    import app.services.topic_summary_service as tss

    svc, session, Session = _setup()
    _seed(svc, session)
    ts = _svc(Session, FakeLLM())
    # 路由内部走全局单例(默认 SessionLocal),测试中替换为绑定内存库的实例
    monkeypatch.setattr(tss, "get_topic_summary_service", lambda: ts)

    # 读缓存:无缓存 found=false
    got = api_topic_summary(topic="音爆", db=session)
    assert got["found"] is False

    # 强制生成(走线程池,同步等结果)
    out = asyncio.run(api_topic_summary_regenerate(topic="音爆", db=session))
    assert out["generated_now"] is True and out["status"] == "排查中"
    got = api_topic_summary(topic="音爆", db=session)
    assert got["found"] is True and got["stale"] is False

    # 不存在的主题 404
    try:
        api_topic_summary(topic="不存在", db=session)
        raise AssertionError("应 404")
    except HTTPException as e:
        assert e.status_code == 404
    print("api summary read/force OK")


if __name__ == "__main__":
    test_generate_topic_json_fields()
    test_fingerprint_fresh_cache_no_llm()
    test_fingerprint_stale_regenerates()
    test_parse_failure_fallback()
    test_status_out_of_enum()
    test_insufficient_text_skip()
    test_run_round_lock_and_release()
    test_run_round_idempotent()
    test_get_cached_missing_and_fresh()
    test_api_topics_stats()
    test_api_topic_summary_read_and_force()
    print("ALL TOPIC SUMMARY TESTS PASSED")
