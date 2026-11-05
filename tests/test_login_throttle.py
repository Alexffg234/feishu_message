"""H2-C 登录限流 DB 化测试。

验证:失败计数存储于 login_fails 表(跨 worker/实例共享),
连续失败超限锁定、窗口过期解锁、成功登录 reset;
DB 不可用时降级为进程内 dict,不阻断登录主流程。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, LoginFail
from app.services.auth_service import LoginThrottle


class FakeClock:
    """可推进的假时钟,替代 time.time,避免真实 sleep。"""

    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _shared_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _throttle(Session, clock: FakeClock):
    return LoginThrottle(
        max_fails=3, window=300, session_factory=Session, now_func=clock
    )


def test_lock_after_max_fails():
    Session = _shared_session()
    th = _throttle(Session, FakeClock())
    assert th.locked("alice") is False
    th.record_fail("alice")
    th.record_fail("alice")
    assert th.locked("alice") is False  # 2 < 3
    th.record_fail("alice")
    assert th.locked("alice") is True   # 3 >= 3
    th.reset("alice")
    assert th.locked("alice") is False  # 成功登录清空


def test_window_expiry_unlocks():
    Session = _shared_session()
    clock = FakeClock()
    th = LoginThrottle(
        max_fails=2, window=300, session_factory=Session, now_func=clock
    )
    th.record_fail("bob")
    th.record_fail("bob")
    assert th.locked("bob") is True
    clock.advance(301)                  # 窗口过期
    assert th.locked("bob") is False
    th.record_fail("bob")               # 过期后重新计数(1 次)
    assert th.locked("bob") is False


def test_cross_instance_shared_count():
    """两个实例(模拟两 worker)共享同一 DB 失败计数。"""
    Session = _shared_session()
    clock = FakeClock()
    th1 = LoginThrottle(
        max_fails=2, window=300, session_factory=Session, now_func=clock
    )
    th2 = LoginThrottle(
        max_fails=2, window=300, session_factory=Session, now_func=clock
    )
    th1.record_fail("carol")
    th1.record_fail("carol")
    # 另一个 worker 实例看到同一份计数,同样锁定
    assert th2.locked("carol") is True
    # 且表里有记录(classic 防丢)
    with Session() as s:
        row = s.get(LoginFail, "carol")
        assert row is not None and row.fail_count == 2


def test_fallback_to_memory_when_db_down():
    """DB 不可用时降级为进程内 dict,不抛异常、不阻断登录校验。"""
    def bad_factory():  # 每次打开会话都抛错
        raise RuntimeError("db down")

    clock = FakeClock()
    th = LoginThrottle(
        max_fails=2, window=300, session_factory=bad_factory, now_func=clock
    )
    assert th.locked("dave") is False
    th.record_fail("dave")
    th.record_fail("dave")
    assert th.locked("dave") is True
    th.reset("dave")
    assert th.locked("dave") is False


def test_record_fail_concurrent_atomic_increment():
    """20 线程并发对同一新 key 各记一次失败,持久化计数必须恰为 20。

    回归:旧实现是非原子读-改-写,并发首次 record_fail 会因主键冲突被
    `except Exception` 误判为 DB down 而只落到进程内兜底,持久化计数被
    严重低估(曾复现 fail_count=2)。修复后原子递增须跨线程/worker 可见。

    用「每线程独立连接」的文件式 SQLite(非共享单连接),才能真实复现
    多 worker 并发;SQLite 写锁由 busy timeout 串行化,正确性仍成立。
    """
    import os
    import tempfile
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.services.auth_service import LoginThrottle

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=NullPool,  # 每个线程拿到独立连接
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        clock = FakeClock()
        th = LoginThrottle(
            max_fails=1000, window=300, session_factory=Session, now_func=clock
        )
        key = "concurrent_user"
        n = 20
        barrier = threading.Barrier(n)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()  # 尽量同时起跑,放大竞态窗口
                th.record_fail(key)
            except BaseException as exc:  # noqa: BLE001 - 收集异常以便断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发 record_fail 不应抛异常: {errors}"
        with Session() as s:
            row = s.get(LoginFail, key)
            assert row is not None and row.fail_count == n, (
                f"持久化计数应为 {n},实际 {row.fail_count if row else None}"
            )
        # 未误走 mem 兜底:该 key 不应出现在进程内 dict
        assert key not in th._mem
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass
