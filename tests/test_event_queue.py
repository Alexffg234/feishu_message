"""事件队列(先应答后处理)测试。

内存模式:异步消费且保序、失败退避重试后成功、重试耗尽写死信、
失败不阻塞后续事件、队列满返回 False、关停排空在途事件、
H6 修复后退避重试在队列持续繁忙时不被饿死。

outbox 持久化模式:submit 先落库、消费后标 done、重复 key 幂等去重、
失败退避重试并最终 done、worker「重启」(新队列实例)后恢复处理未完成事件。
"""
import json
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.event_queue import EventQueue

# 持久化模式的临时库目录,进程退出自动清理。ignore_cleanup_errors:
# Windows 下 explorer/杀软可能仍持有 .db 句柄,清理失败不该变成测试报错。
_TMP_DIR = tempfile.TemporaryDirectory(
    prefix="feishu-eq-", ignore_cleanup_errors=True
)


def _wait(cond, timeout: float = 8.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _make_event() -> "P2ImMessageReceiveV1":
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_q1"}, "sender_type": "user"},
                "message": {
                    "message_id": "om_q1",
                    "chat_id": "oc_q",
                    "message_type": "text",
                    "create_time": 1700000000000,
                    "content": '{"text":"hello queue"}',
                    "parent_id": "",
                    "root_id": "",
                    "mentions": [],
                },
            }
        }
    )


def _make_session_factory():
    """每次调用一个独立的**文件型** SQLite,每次 session 独占一条连接。

    不能用 `sqlite://` + StaticPool:那会让消费者线程和测试线程**共用同一条
    连接**,测试线程每关一次 session 就把消费者正在写的事务 rollback 掉。症状
    有三种,全是假故障:`cannot commit - no transaction is active`(提交报错)、
    commit 静默变空操作(行卡在 processing 等租约过期 60s)、或异常冒到
    `_claim_next` 把消费者线程打死。生产用 MySQL 连接池,每个 session 独占连接,
    这些都不会发生——测试环境自己造出来的故障只会掩盖真问题。
    文件型 + 唯一文件名同时保证用例之间互相隔离。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base, EventOutbox

    db = Path(_TMP_DIR.name) / f"outbox_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session, EventOutbox


# ============================================================ 内存模式
def test_fifo_consume():
    got: list = []
    q = EventQueue(process_fn=lambda x: got.append(x), retry_delays=(0.01,))
    q.start()
    for i in range(5):
        assert q.submit(i)
    assert _wait(lambda: len(got) == 5)
    q.stop()
    assert got == [0, 1, 2, 3, 4]


def test_retry_then_success(tmp_path):
    calls = {"n": 0}
    got: list = []

    def fn(x):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        got.append(x)

    dl = str(tmp_path / "d.jsonl")
    q = EventQueue(process_fn=fn, retry_delays=(0.01, 0.01), dead_letter=dl)
    q.start()
    q.submit("a")
    assert _wait(lambda: got == ["a"])
    q.stop()
    assert calls["n"] == 3
    assert not Path(dl).exists()


def test_dead_letter_after_retries(tmp_path):
    dl = tmp_path / "d.jsonl"

    def fn(x):
        raise RuntimeError("always down")

    q = EventQueue(process_fn=fn, retry_delays=(0.01, 0.01), dead_letter=str(dl))
    q.start()
    q.submit({"mid": "m1"})
    assert _wait(lambda: dl.exists() and dl.read_text(encoding="utf-8").strip())
    q.stop()
    line = json.loads(dl.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["event"]["mid"] == "m1"
    assert line["error"] == "重试耗尽"


def test_failure_does_not_block_queue(tmp_path):
    got: set = set()

    def fn(x):
        if x == "bad":
            raise RuntimeError("down")
        got.add(x)

    q = EventQueue(process_fn=fn, retry_delays=(0.05, 0.05), dead_letter=str(tmp_path / "d.jsonl"))
    q.start()
    q.submit("bad")
    q.submit("good1")
    q.submit("good2")
    assert _wait(lambda: {"good1", "good2"} <= got)
    q.stop()
    assert "bad" not in got


def test_submit_full_returns_false():
    started = threading.Event()
    release = threading.Event()

    def fn(x):
        started.set()
        release.wait(timeout=5)

    q = EventQueue(process_fn=fn, maxsize=1)
    q.start()
    assert q.submit("blocker")  # 消费者正卡在它上面
    assert _wait(lambda: started.is_set())
    assert q.submit("filler")  # 占满唯一的空位
    assert not q.submit("overflow")
    release.set()
    q.stop()


def test_stop_drains_pending():
    got: list = []
    q = EventQueue(process_fn=lambda x: got.append(x))
    q.start()
    for i in range(10):
        q.submit(i)
    q.stop(drain=True)
    assert sorted(got) == list(range(10))


def test_h6_no_retry_starvation():
    """H6:队列持续繁忙(几乎不空闲)时,退避重试仍能按期被调度,不被饿死。

    修复前,退避重试只在「队列恰好为空」时刷新;若队列被持续灌满,
    重试任务会无限期延迟。修复后每次消费都带超时刷新,本测试断言
    在持续灌入 filler 的压力下 retry_target 最终仍被处理。
    """
    state = {"n": 0}
    got: list = []

    def fn(x):
        if x == "retry_target":
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("first")
            got.append(x)
            return
        # filler:消耗一点时间,模拟真实入库耗时,让队列更难空闲
        time.sleep(0.002)
        got.append(x)

    q = EventQueue(process_fn=fn, retry_delays=(0.02,))
    q.start()
    q.submit("retry_target")
    end = time.monotonic() + 0.6
    i = 0
    while time.monotonic() < end:
        if not q.submit(("filler", i)):
            break
        i += 1
        time.sleep(0.001)
    assert _wait(lambda: "retry_target" in got), "退避重试被饿死"
    q.stop()


def test_queue_drives_message_pipeline(tmp_path):
    """集成(内存模式):队列消费的事件走正常入库管道,落库成功。

    用「每连接独立」的文件式 SQLite(而非共享单连接 StaticPool):消费线程
    写、测试线程读各用独立连接,避免静态单连接上跨线程读写竞态(曾触发
    StaleDataError/NoResultFound 偶发失败)。
    """
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.models import Base, Message
    from app.services.message_service import MessageService

    db_path = tmp_path / "pipeline.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def fn(data):
        with Session() as s:
            MessageService(s, allowed_chat_ids=set()).handle_receive_event(data)

    q = EventQueue(process_fn=fn, retry_delays=(0.01,))
    try:
        q.start()
        q.submit(_make_event())
        assert _wait(lambda: _count_messages(Session) == 1)
        with Session() as s:
            row = s.query(Message).one()
            assert row.text == "hello queue"
    finally:
        q.stop()
        engine.dispose()
        try:
            os.remove(db_path)
        except OSError:
            pass


def _count_messages(Session) -> int:
    from app.models import Message

    with Session() as s:
        return s.query(Message).count()


# ============================================================ outbox 持久化模式
def _outbox_rows(Session):
    from app.models import EventOutbox

    with Session() as s:
        return s.query(EventOutbox).order_by(EventOutbox.id).all()


def test_persistent_submit_then_done():
    Session, _ = _make_session_factory()
    got: list = []

    def fn(data):
        got.append(data)
        with Session() as s:
            from app.models import Message

            s.add(Message(message_id="om_q1", chat_id="oc_q", text="hi",
                          create_time=1700000000000))
            s.commit()

    q = EventQueue(process_fn=fn, retry_delays=(0.01,), session_factory=Session)
    q.start()
    assert q.submit(_make_event())
    assert _wait(lambda: _outbox_rows(Session) and _outbox_rows(Session)[0].status == "done")
    q.stop()
    rows = _outbox_rows(Session)
    assert len(rows) == 1
    assert rows[0].status == "done"


def test_persistent_duplicate_key_dedup():
    Session, _ = _make_session_factory()
    q = EventQueue(process_fn=lambda x: None, session_factory=Session)
    # 同一 message_id → 同一 (chat_id, message_id) 去重键
    ev = _make_event()
    assert q.submit(ev)
    assert q.submit(ev)  # 幂等:唯一键冲突被吞并,仍返回 True
    rows = _outbox_rows(Session)
    assert len(rows) == 1


def test_persistent_retry_then_success():
    Session, _ = _make_session_factory()
    state = {"n": 0}

    def fn(data):
        if state["n"] < 2:
            state["n"] += 1
            raise RuntimeError("down")
        state["n"] += 1

    q = EventQueue(process_fn=fn, retry_delays=(0.01, 0.05), session_factory=Session)
    q.start()
    q.submit(_make_event())
    assert _wait(lambda: _outbox_rows(Session) and _outbox_rows(Session)[0].status == "done")
    q.stop()
    row = _outbox_rows(Session)[0]
    assert row.status == "done"
    assert row.attempt == 2  # 失败 2 次(retrying 2 次)后第 3 次成功


def test_persistent_consumer_survives_transient_db_error(monkeypatch, caplog):
    """一次瞬时 DB 异常不得打死消费者线程。

    生产上这类异常是常态:死锁(1213)、锁等待超时(1205)、连接被服务端掐断、
    主从切换。原来 `_run_persistent` 没有护栏,异常直接从 `_claim_next` 冒出去终结
    线程,队列**永久停摆**,现场只剩 threading 打的一行 stderr——监控上看不出
    「队列停了」,比丢一条消息严重得多。护栏之后靠租约 + 重试自愈。
    """
    import logging

    from sqlalchemy.exc import OperationalError

    Session, _ = _make_session_factory()
    got: list = []
    q = EventQueue(process_fn=lambda x: got.append(x), session_factory=Session)

    real_claim = q._claim_next
    calls = {"n": 0}

    def flaky_claim():
        if calls["n"] == 0:
            calls["n"] += 1
            raise OperationalError("SELECT 1", {}, Exception("deadlock"))
        return real_claim()

    monkeypatch.setattr(q, "_claim_next", flaky_claim)
    caplog.set_level(logging.ERROR, logger="app.services.event_queue")

    q.start()
    try:
        assert q.submit(_make_event())
        assert _wait(lambda: got, timeout=5), "瞬时异常之后事件再也没被处理:消费者线程死了"
        assert q._thread is not None and q._thread.is_alive()
    finally:
        q.stop()

    assert calls["n"] == 1
    assert "消费循环异常" in caplog.text


def test_persistent_survives_restart():
    """worker「重启」恢复:事件先落库,即使消费者从未运行/中断,
    新队列实例启动后仍能从 outbox 取出并处理,不丢失。"""
    Session, _ = _make_session_factory()

    # 第一个实例只负责落库(模拟回调落库后未及处理即崩溃)
    q1 = EventQueue(process_fn=lambda x: None, session_factory=Session)
    assert q1.submit(_make_event())
    assert len(_outbox_rows(Session)) == 1
    q1.stop()  # 未 start,纯粹写入

    # 第二个实例接管消费,应恢复 pending 事件
    got: list = []
    q2 = EventQueue(process_fn=lambda x: got.append(x), session_factory=Session)
    q2.start()
    assert _wait(lambda: got)
    assert _wait(lambda: _outbox_rows(Session)[0].status == "done")
    q2.stop()


def test_persistent_crash_reclaims_processing():
    """worker 处理中崩溃:若行停在 processing 且租约已过期,新实例应回收重处理。"""
    Session, _ = _make_session_factory()

    # 模拟:事件已落库并进入 processing,但持有方在提交结果前崩溃(租约过期)
    from app.models import EventOutbox

    q1 = EventQueue(process_fn=lambda x: None, session_factory=Session)
    assert q1.submit(_make_event())
    with Session() as s:
        s.query(EventOutbox).update(
            {
                EventOutbox.status: "processing",
                EventOutbox.retry_at_ms: int(time.time() * 1000) - 5000,  # 租约已过期
            }
        )
        s.commit()

    got: list = []
    q2 = EventQueue(process_fn=lambda x: got.append(x), session_factory=Session,
                    claim_lease_ms=60_000)
    q2.start()
    assert _wait(lambda: got)
    assert _wait(lambda: _outbox_rows(Session)[0].status == "done")
    q2.stop()


# ============================================================ 处理租约(心跳续期)
def test_slow_task_keeps_lease_and_is_not_stolen():
    """慢任务靠心跳续租,不该被别的 worker 当成「持有者崩了」抢走重跑。

    处理耗时超过租约时,表里「处理慢」和「持有者死了」长得一模一样。不续租
    就会被抢:数据不丢(幂等键兜底),但会重复下载/上传媒体、attempt 虚增、
    两条日志互相打架。这里用「租约 300ms + 任务卡住 1s」把时间压缩到可测。
    """
    Session, _ = _make_session_factory()
    started = threading.Event()
    release = threading.Event()
    done: list = []

    def slow(data):
        started.set()
        release.wait(5)
        done.append(data)

    q = EventQueue(
        process_fn=slow, session_factory=Session, claim_lease_ms=300
    )
    q.start()
    q2 = EventQueue(process_fn=lambda x: None, session_factory=Session, claim_lease_ms=300)
    try:
        assert q.submit(_make_event())
        assert started.wait(5), "事件没有被认领处理"

        # 连续尝试 1.2s(4 个租约周期),每一次都应抢不到
        for _ in range(6):
            assert q2._claim_next() is None, "慢任务被别的 worker 抢走了:心跳没续上租约"
            time.sleep(0.2)

        release.set()
        assert _wait(lambda: _outbox_rows(Session)[0].status == "done")
    finally:
        release.set()
        q.stop()


def test_lease_keeper_reports_lost_and_stops_renewing():
    """租约被回收后必须停止续租,不能把对方的租约改成自己的。

    续租是无条件写就会出这个事故:A 卡住 → B 回收重跑 → A 恢复后又把租约写成
    「A 的到期时间」,两边都以为自己持有,谁都判不出来。所以续租必须是
    CAS(`WHERE retry_at_ms = 上次写入值`)。
    """
    from app.models import EventOutbox
    from app.services.event_queue import _LeaseKeeper

    Session, _ = _make_session_factory()
    q = EventQueue(process_fn=lambda x: None, session_factory=Session, claim_lease_ms=200)
    assert q.submit(_make_event())
    row = q._claim_next()
    assert row is not None

    keeper = _LeaseKeeper(Session, row, lease_ms=200)
    keeper.start()
    try:
        # 模拟另一个 worker 把行回收了(把租约改成它自己的到期时刻)
        robbed = int(time.time() * 1000) + 999_000
        with Session() as s:
            s.query(EventOutbox).update({EventOutbox.retry_at_ms: robbed})
            s.commit()
        # 等心跳跑过几轮
        assert _wait(lambda: keeper.lost(), timeout=3), "租约被抢后没有上报 lost"
        with Session() as s:
            val = s.query(EventOutbox).first().retry_at_ms
        assert val == robbed, "租约被回收后仍在续租,把对方的租约改成了自己的"
    finally:
        keeper.stop()
