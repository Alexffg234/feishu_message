"""标签指标页(/metrics)+ 指标端点扩展字段的测试。

为什么单独测这两块:
1. 页面是这套度量的唯一消费入口。指标端点只给 curl 看时没人看,而页面缺
   文件/少引资源 = 整条观测链路静默失效,所以锁「三个文件都在 + 引用真实存在 +
   导航有入口」,和 /review 一个口径。
2. `standard_coverage.labels` / `off_standard` 是这次新加的结构,它把「体系漏洞」
   拆成两半:挂着没人用的标准标签(冗余)、用了却不在册的标签(别名漏网)。
   这两半最容易算错 —— 一个要多算 0 值标签,一个要精确排除标准集。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.models.topic_group import TopicGroup

CHAT = "oc_metrics"
T0 = 1700000000000


def _setup(groups):
    """groups: [(大类名, [标签...]), ...] —— 按 sort_order 顺序落库。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for i, (name, topics) in enumerate(groups):
        session.add(TopicGroup(name=name, parent="", topics=list(topics), sort_order=i))
    session.commit()
    return session


def _group(session, root, topic, texts=None):
    texts = texts or [f"{root} 的消息"]
    for i, text in enumerate(texts):
        session.add(
            Message(
                message_id=f"{root}-{i}",
                chat_id=CHAT,
                sender_id=f"u{i}",
                sender_type="user",
                message_type="text",
                text=text,
                create_time=str(T0 + i * 60_000),
                root_message_id=root,
                topic=topic,
                topic_conf=1.0 if topic else 0.0,
            )
        )
    session.add(
        Conversation(
            chat_id=CHAT, root_message_id=root, topic=topic, message_count=len(texts)
        )
    )
    session.commit()


def _metrics(session):
    from app.api.routes import api_topic_metrics

    return api_topic_metrics(db=session)


# ---------------------------------------------------------------- 页面
def test_metrics_page_assets_exist():
    """页面缺失时 /metrics 直接 500,而这是整套指标的唯一可视化入口。"""
    from app.api.routes import STATIC_DIR

    for name in ("metrics.html", "metrics.js", "metrics.css"):
        assert (STATIC_DIR / name).is_file(), f"缺少 app/static/{name}"

    html = (STATIC_DIR / "metrics.html").read_text(encoding="utf-8")
    # 页面引用的资源必须真实存在,否则浏览器 404 后页面一片空白且没有报错
    for ref in ("metrics.css", "metrics.js", "nav.js", "components.css"):
        assert ref in html, f"metrics.html 没有引用 {ref}"
        assert (STATIC_DIR / ref).is_file(), f"metrics.html 引用了不存在的 {ref}"


def test_nav_links_to_metrics_page():
    """导航里没入口 = 页面上线了也没人找得到。"""
    from app.api.routes import STATIC_DIR

    nav = (STATIC_DIR / "nav.js").read_text(encoding="utf-8")
    assert "/metrics" in nav


def test_metrics_route_serves_html():
    """路由返回的是 FileResponse(不是字符串),所以查 path 而不是 body。"""
    from app.api.routes import metrics_page

    resp = metrics_page()
    assert resp.status_code == 200
    assert str(resp.path).replace("\\", "/").endswith("static/metrics.html")


# ------------------------------------------------- 标准标签用量(含 0 值)
def test_standard_usage_lists_every_standard_including_unused():
    """每个标准标签都要出现,没被用过的必须是 0 而不是「不出现」。

    这条是刻意的:页面靠「0 组」这个显式值来标出标准集里的冗余项,
    漏掉它们等于把这个信号抹了。
    """
    s = _setup([
        ("画质类问题", ["画面模糊", "黑屏", "花屏", "闪屏"]),
        ("语音类问题", ["语音唤醒无反应"]),
    ])
    _group(s, "g_blur", "画面模糊")
    _group(s, "g_black1", "黑屏")
    _group(s, "g_black2", "黑屏")

    sc = _metrics(s)["standard_coverage"]
    by_label = {x["label"]: x["groups"] for x in sc["labels"]}

    assert set(by_label) == {"画面模糊", "黑屏", "花屏", "闪屏", "语音唤醒无反应"}
    assert by_label["黑屏"] == 2  # 两个组
    assert by_label["画面模糊"] == 1
    assert by_label["花屏"] == 0  # 没用过也要在
    assert set(sc["unused_standards"]) == {"花屏", "闪屏", "语音唤醒无反应"}
    # 没被用到的标签排序要排在后面,否则冗余项会淹没主力标签
    assert all(x["groups"] == 0 for x in sc["labels"][-3:])
    assert sc["standard_count"] == 5
    assert sc["used_standard_count"] == 2


def test_standard_usage_carries_group_name():
    """大类名要带上,页面靠它分组;两层结构按「大类 / 子类」拼。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(TopicGroup(name="画质类问题", parent="", topics=["黑屏"], sort_order=0))
    session.add(
        TopicGroup(
            name="画面模糊类问题", parent="画质类问题", topics=["字幕看不清"], sort_order=1
        )
    )
    session.commit()
    _group(session, "g1", "黑屏")
    _group(session, "g2", "字幕看不清")

    by_label = {
        x["label"]: x["group"] for x in _metrics(session)["standard_coverage"]["labels"]
    }
    assert by_label["黑屏"] == "画质类问题"
    assert by_label["字幕看不清"] == "画质类问题 / 画面模糊类问题"


# ------------------------------------------------------- 非标准标签漏网
def test_off_standard_lists_used_labels_outside_standards():
    s = _setup([("画质类问题", ["画面模糊", "黑屏"])])
    _group(s, "g_ok", "画面模糊")
    _group(s, "g_bad1", "画质模糊推送升级")
    _group(s, "g_bad2", "画质模糊推送升级")

    sc = _metrics(s)["standard_coverage"]
    off = {x["label"]: x["groups"] for x in sc["off_standard"]}

    assert off == {"画质模糊推送升级": 2}
    assert sc["off_standard_count"] == 1
    # 标准标签绝不能出现在漏网清单里(它是「不在册」的定义)
    assert "画面模糊" not in off


def test_off_standard_empty_when_everything_converged():
    s = _setup([("画质类问题", ["画面模糊", "黑屏"])])
    _group(s, "g1", "画面模糊")
    _group(s, "g2", "黑屏")

    sc = _metrics(s)["standard_coverage"]
    assert sc["off_standard"] == []
    assert sc["off_standard_count"] == 0


def test_off_standard_ignores_unlabeled_groups():
    """未分类组(topic='')不是「非标准标签」,不能被算进漏网清单。"""
    s = _setup([("画质类问题", ["画面模糊"])])
    _group(s, "g_ok", "画面模糊")
    _group(s, "g_none", "")

    sc = _metrics(s)["standard_coverage"]
    assert sc["off_standard"] == []
    # 未分类另有一块指标,别混进来
    assert _metrics(s)["unclassified"]["groups"] == 1


def test_off_standard_capped_but_count_is_full():
    """清单最多 50 条,但 count 永远是全量 —— 否则页面显示的「共 N 个」会骗人。"""
    s = _setup([("画质类问题", ["画面模糊"])])
    _group(s, "g_ok", "画面模糊")
    for i in range(55):
        _group(s, f"g_x{i}", f"自造标签{i}")

    sc = _metrics(s)["standard_coverage"]
    assert len(sc["off_standard"]) == 50
    assert sc["off_standard_count"] == 55


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
