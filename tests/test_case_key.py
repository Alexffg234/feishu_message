"""设备标识锚点测试(阶段二:case_key 抽取 + 缝合确定性通道)。

覆盖:
- extract_case_key:UUID / SN(带标签) / MAC(排除全0/全F) / 公网IP(排除私有),未命中为空
- 入库时 case_key 自动生成
- conv_stitch 确定性通道:同 case_key 会话直接合并(不调 LLM),仍过标签冲突护栏
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation
from app.schemas.message import MessageCreate
from app.services.case_key import extract_case_key
from app.services.conv_stitch import plan_stitches
from app.services.message_service import MessageService

T0 = 1700000000000
CHAT = "oc_case"
UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        return "无"  # 不应被调用


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session


def _msg(mid, sender, ts, text, parent="", root=""):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=text,
        create_time=str(ts),
        parent_message_id=parent,
        root_message_id=root,
    )


# ---------------------------------------------------------------- 抽取
def test_extract_uuid():
    assert extract_case_key(f"设备 UUID 是 {UUID},请登记") == "uuid:" + UUID.upper()
    print("uuid OK")


def test_extract_sn_with_label():
    assert extract_case_key("SN:HTX5432100A 异常") == "sn:HTX5432100A"
    assert extract_case_key("序列号 8877665544,开机问题") == "sn:8877665544"
    # 无标签的数字串不是 SN
    assert extract_case_key("用了 12345 这个号码") == ""
    print("sn OK")


def test_extract_mac():
    assert extract_case_key("mac 地址 00:1A:2B:3C:4D:5E 连不上") == "mac:00-1A-2B-3C-4D-5E"
    # 全 0 / 全 F 视为无效
    assert extract_case_key("网卡 00:00:00:00:00:00 报错") == ""
    assert extract_case_key("地址 ff:ff:ff:ff:ff:ff 广播") == ""
    print("mac OK")


def test_extract_ip_public_only():
    assert extract_case_key("公网 IP:8.8.8.8 无法访问") == "ip:8.8.8.8"
    # 私有/回环不采信
    assert extract_case_key("内网地址 192.168.1.100") == ""
    assert extract_case_key("127.0.0.1") == ""
    print("ip OK")


def test_extract_unknown_empty():
    assert extract_case_key("投屏又失败了,重启试试") == ""
    assert extract_case_key("") == ""
    print("unknown OK")


# ---------------------------------------------------------------- 入库自动生成
def test_save_generates_case_key():
    svc, session = _setup()
    svc.save_message(
        _msg("m1", "u1", T0, f"这台机器 UUID {UUID} 又出问题了")
    )
    from app.dao.message_repo import MessageRepo

    m = MessageRepo(session).get_by_message_id(CHAT, "m1")
    assert m.case_key == "uuid:" + UUID.upper()
    print("save case_key OK")


# ---------------------------------------------------------------- 缝合确定性通道
def test_stitch_same_case_key_no_llm():
    svc, session = _setup()
    # 同一 UUID 的三个独立会话(时间拉开,无 LLM 也能靠 case_key 合并)
    svc.save_message(_msg("a", "u1", T0, f"开机报警,设备 {UUID}"))
    svc.save_message(_msg("b", "u2", T0 + 3600_000, f"继续排查,{UUID}"))
    svc.save_message(_msg("c", "u1", T0 + 7200_000, f"{UUID} 已修复,结案"))
    assert len(session.query(Conversation).all()) == 3

    llm = FakeLLM()
    pairs = plan_stitches(session, CHAT, llm)
    # 三个会话都挂同一 case_key → 全部并入最早会话 a
    assert len(pairs) == 2, pairs
    assert sorted(p[0] for p in pairs) == [2, 3]
    assert pairs[0][1] == 1 and pairs[1][1] == 1
    # 确定性通道命中 → 不调 LLM
    assert llm.calls == 0, f"不应调用 LLM,实际 {llm.calls} 次"
    print("stitch deterministic OK")


def test_stitch_case_key_different_no_merge():
    svc, session = _setup()
    svc.save_message(_msg("a", "u1", T0, f"设备A {UUID} 异常"))
    svc.save_message(_msg("b", "u2", T0 + 3600_000, "设备B 的 SN:99XYYZZ6600 也异常"))
    llm = FakeLLM()
    pairs = plan_stitches(session, CHAT, llm)
    # 两个不同设备标识,不合并;且确定性通道未命中尾部窗口,但窗口长度<2 不跑 LLM
    assert pairs == []
    print("stitch different OK")


def test_stitch_case_key_topic_conflict_still_guarded():
    svc, session = _setup()
    from app.dao.conversation_repo import ConversationRepo
    from app.dao.message_repo import MessageRepo

    svc.save_message(_msg("a", "u1", T0, f"设备 {UUID} 开不了机"))
    svc.save_message(_msg("b", "u2", T0 + 3600_000, f"{UUID} 自动重启"))
    # 强行给两个会话打不同 topic,标签冲突护栏应拦截确定性合并
    repo, convo = MessageRepo(session), ConversationRepo(session)
    repo.set_topic_for_group(CHAT, "a", "开机问题")
    repo.set_topic_for_group(CHAT, "b", "自动重启")
    convo.set_topic(CHAT, "a", "开机问题")
    convo.set_topic(CHAT, "b", "自动重启")
    session.commit()

    llm = FakeLLM()
    pairs = plan_stitches(session, CHAT, llm)
    assert pairs == [], f"标签冲突应拦截,实际 {pairs}"
    print("stitch conflict guard OK")


if __name__ == "__main__":
    test_extract_uuid()
    test_extract_sn_with_label()
    test_extract_mac()
    test_extract_ip_public_only()
    test_extract_unknown_empty()
    test_save_generates_case_key()
    test_stitch_same_case_key_no_llm()
    test_stitch_case_key_different_no_merge()
    test_stitch_case_key_topic_conflict_still_guarded()
    print("ALL CASE KEY TESTS PASSED")
