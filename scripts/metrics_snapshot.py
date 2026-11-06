#!/usr/bin/env python3
"""标签体系改造前后的指标快照与对比。

为什么需要这个
--------------
标签体系瘦身(41 → 33)会让一批指标同时变化,而且**方向不一样**:

- 标准标签数、库内标签数、非标准标签漏网 —— 应该下降,这是改造目的;
- 未分类**率** —— 会先**上升**,因为动作词标签(换机推送/已推/上门维修)被清空,
  它们原本的组回到未分类队列。这是**预期代价**,不是系统退化;
- 标准标签覆盖率 —— 分母从 41 变 33,即使一个标签都没多用,覆盖率也会自动上升。
  拿它证明「分类变准了」是自欺欺人。

把这几行数字并排一放,不解释方向,做出来的报告就是在误导人。所以每一行都标出
方向:✓ 改善 / ✗ 恶化 / · 预期 / · 参考 / 持平,口径变了的项单独列一段说明
不能直接比,进程内累计的项直接排除在快照之外。

关键约束:「前」的快照只能在 apply 那一刻抓
-------------------------------------------
apply 之后 `messages.topic` 已被改写,「前」的状态在库里不复存在(只剩 backup
json 里的原始 topic,要重算指标就得整库回滚)。所以 `slim_topics.py --apply`
会在写库**之前**自动抓一份 before 快照,不依赖人记得先跑一次。

用法
----
    python scripts/metrics_snapshot.py --save before      # 手动抓一份(可选,apply 会自动抓)
    python scripts/metrics_snapshot.py --save after       # apply 之后再抓
    python scripts/metrics_snapshot.py --diff data/metrics-before-*.json data/metrics-after-*.json
    python scripts/metrics_snapshot.py --list             # 列出现有快照
    python scripts/metrics_snapshot.py                    # 打印当前指标摘要(不落盘)
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCHEMA = 1
DEFAULT_DIR = Path("data")


# ---------------------------------------------------------------- 文本对齐
# 报表里中英文混排,用 f"{s:<20}" 会被 CJK 字符撑歪(一个字占两列)。
# 所有列宽都按**显示宽度**算。
def _dw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _dw(s))
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _pp(x) -> str:
    """比率的变化用「百分点」表达:12.5% → 26.1% 是 +13.6pp,不是 +13.6%。"""
    return f"{float(x) * 100:+.1f}pp"


# ---------------------------------------------------------------- 抓取
def capture(session, label: str = "", plan: dict | None = None) -> dict:
    """把指标端点的核心字段摊平成可长期存储、可逐项对比的形状。

    只取**跨时间可比**的字段。三个进程内累计指标(normalization / recall /
    stitching)刻意不取:它们重启归零,两次快照之间大概率重启过,比出来的差值
    没有任何含义。
    """
    # 延迟 import:这些是 FastAPI 路由侧的依赖,脚本不该在 import 期就拉起整个应用
    from app.api.routes import api_topic_metrics

    m = api_topic_metrics(db=session)
    sc = m.get("standard_coverage") or {}
    un = m.get("unclassified") or {}
    rv = m.get("review") or {}
    conf = m.get("confidence") or {}

    return {
        "schema": SCHEMA,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "label": label,
        # 标准体系(判断「口径有没有变」的依据)
        "standard_count": int(sc.get("standard_count") or 0),
        "used_standard_count": int(sc.get("used_standard_count") or 0),
        "topic_count": int(m.get("topic_count") or 0),
        "groups": int(un.get("total_groups") or 0),
        # 指标
        "standard_rate": float(sc.get("rate") or 0.0),
        "off_standard_count": int(sc.get("off_standard_count") or 0),
        "unclassified_groups": int(un.get("groups") or 0),
        "unclassified_rate": float(un.get("rate") or 0.0),
        "low_frequency_ratio": float(m.get("low_frequency_ratio") or 0.0),
        "low_frequency_topic_count": int(m.get("low_frequency_topic_count") or 0),
        "merge_rate": float(m.get("merge_rate") or 0.0),
        "remaining_fragmented": int(m.get("remaining_fragmented_topics") or 0),
        "conf_high": int(conf.get("high") or 0),
        "conf_medium": int(conf.get("medium") or 0),
        "conf_low": int(conf.get("low") or 0),
        "conf_none": int(conf.get("none") or 0),
        "pending_groups": int(rv.get("pending_groups") or 0),
        "gold_groups": int(rv.get("gold_groups") or 0),
        # 明细:对比「哪些标签消失了 / 冒出来了」靠这两张表
        "labels": {x["label"]: int(x["groups"]) for x in (sc.get("labels") or [])},
        "off_standard": {
            x["label"]: int(x["groups"]) for x in (sc.get("off_standard") or [])
        },
        # 由 slim_topics --apply 塞进来的方案摘要(手动 --save 时为空)
        "plan": plan or {},
    }


def save(session, label: str, out_dir: Path, plan: dict | None = None) -> Path:
    data = capture(session, label=label, plan=plan)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in (label or "snap") if ch.isalnum() or ch in "-_") or "snap"
    path = out_dir / f"metrics-{safe}-{stamp}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(data.get("schema") or 0) != SCHEMA:
        raise ValueError(
            f"{path} 的快照格式版本是 {data.get('schema')},当前脚本只认 {SCHEMA}。"
            "格式变过就先重新抓一份基线。"
        )
    return data


# ---------------------------------------------------------------- 对比
# 每行: (key, 中文名, kind)
#   kind=count 直接比差值;kind=rate 用百分点
#   tone 由 _tone() 结合上下文判断,不能写死 —— 同一个「上升」在不同指标上
#   含义完全相反(未分类率上升是坏事,覆盖率上升可能只是分母变小)。
ROWS = [
    ("standard_count", "标准标签数", "count"),
    ("topic_count", "库内用到的标签", "count"),
    ("off_standard_count", "非标准标签漏网", "count"),
    ("unclassified_rate", "未分类率", "rate"),
    ("unclassified_groups", "未分类组数", "count"),
    ("low_frequency_ratio", "低频标签占比", "rate"),
    ("merge_rate", "归并率", "rate"),
    ("remaining_fragmented", "待归并碎片", "count"),
    ("pending_groups", "待复核组", "count"),
    ("gold_groups", "gold set 规模", "count"),
]

TONE_MARK = {
    "good": "✓ 改善",
    "bad": "✗ 恶化",
    "expect": "· 预期",
    "note": "· 参考",
    "flat": "  持平",
}


def _delta(before: dict, after: dict, key: str):
    b = before.get(key)
    a = after.get(key)
    if b is None or a is None:
        return None
    return float(a) - float(b)


def _sc_changed(before: dict, after: dict) -> bool:
    return int(after.get("standard_count") or 0) != int(before.get("standard_count") or 0)


def _cleared_groups(before: dict, after: dict) -> int:
    plan = after.get("plan") or before.get("plan") or {}
    try:
        return int(plan.get("cleared_groups") or 0)
    except (TypeError, ValueError):
        return 0


def _tone(key: str, d: float, before: dict, after: dict) -> str:
    """判断这一行的变化是好是坏、还是只是预期内 / 口径变化。

    这里全部是显式规则,不用「越大越好」这种统一假设 —— 那套假设在这种
    指标体系里几乎每条都是错的。
    """
    if abs(d) < 1e-9:
        return "flat"

    if key == "off_standard_count":
        return "good" if d < 0 else "bad"
    if key == "low_frequency_ratio":
        return "good" if d < 0 else "bad"

    # 归并率 / 碎片数在**体系变更前后不可比**:标签合并会让同一标签下聚集更多
    # 组,(chat, topic) 收敛到单组的比例自然下降,碎片数自然上升。这跟「碎片
    # 治理变差」完全是两件事,判成退化会冤枉改造。体系没变时它们照常可比。
    if key in ("merge_rate", "remaining_fragmented"):
        if _sc_changed(before, after):
            return "note"
        return ("good" if d < 0 else "bad") if key == "remaining_fragmented" else (
            "good" if d > 0 else "bad"
        )

    if key in ("unclassified_rate", "unclassified_groups", "pending_groups"):
        # 未分类上升若是「动作词标签被清空、组回队列」造成的,属于预期代价。
        # 优先用方案自己记的 cleared_groups 判断 —— 比「标准标签数是否减少」
        # 可靠:有些改造清空了标签但总数没变。
        if d > 0 and _cleared_groups(before, after):
            return "expect"
        if d > 0 and int(after.get("standard_count") or 0) < int(
            before.get("standard_count") or 0
        ):
            return "expect"
        return "bad" if d > 0 else "good"

    if key in ("standard_count", "topic_count"):
        # 体系在瘦身时,数量下降本身就是目的;不瘦身时下降要另看
        if d < 0 and int(after.get("standard_count") or 0) <= int(
            before.get("standard_count") or 0
        ):
            return "expect"
        return "note"

    if key == "gold_groups":
        return "good" if d > 0 else "note"

    return "note"


def _explain(key: str, tone: str, before: dict, after: dict, d: float) -> str:
    """给判断配一句人话。没有这句,表格就只是数字并排。

    持平的行一律返回空串:这里踩过坑 —— 早期版本在 key==gold_groups 时
    不看 d 就返回「gold set 变小了」,结果「0 → 0」也照样写了「变小」。
    """
    if abs(d) < 1e-9:
        return ""

    plan = after.get("plan") or before.get("plan") or {}

    if key in ("unclassified_rate", "unclassified_groups", "pending_groups"):
        if tone == "expect":
            cleared = plan.get("cleared_groups")
            tail = f"(清空 {cleared} 组)" if cleared else ""
            return f"动作词标签被清空,组回到队列{tail};去 /review 定标"
        if tone == "bad":
            return "闭集缺现象,或别名没收敛住"
        return "人工定标见效"
    if key == "off_standard_count":
        return "旧名进了别名表,历史输出被收敛" if d < 0 else "有新的非标准标签冒出来"
    if key == "low_frequency_ratio":
        return "同义分裂被收敛" if d < 0 else "同义词又分裂了"
    if key == "standard_count":
        return "体系瘦身" if d < 0 else "标准集扩容"
    if key == "topic_count":
        return "标签合并到位" if d < 0 else "有标签长回来了"
    if key in ("merge_rate", "remaining_fragmented"):
        if _sc_changed(before, after):
            return "标签合并后同一标签下组数变多,此项天然变化,不作退化判断"
        return ("碎片并回原问题块" if d > 0 else "碎片变多") if key == "merge_rate" else (
            "碎片减少" if d < 0 else "碎片增加"
        )
    return ""


def render_diff(before: dict, after: dict) -> str:
    flags: list[str] = []
    out: list[str] = []

    b_at = str(before.get("created_at") or "")[:16].replace("T", " ")
    a_at = str(after.get("created_at") or "")[:16].replace("T", " ")
    out.append("== 快照 ==")
    out.append(
        f"  前 {before.get('label') or '-'}  {b_at}  "
        f"(标准 {before.get('standard_count')} / 库内 {before.get('topic_count')} / "
        f"组 {before.get('groups')})"
    )
    out.append(
        f"  后 {after.get('label') or '-'}  {a_at}  "
        f"(标准 {after.get('standard_count')} / 库内 {after.get('topic_count')} / "
        f"组 {after.get('groups')})"
    )

    # ---- 标签体系变化(先讲清楚「发生了什么」,再看指标) ----
    lb = set((before.get("labels") or {}).keys())
    la = set((after.get("labels") or {}).keys())
    gone = sorted(lb - la, key=lambda x: -(before["labels"].get(x) or 0))
    new = sorted(la - lb, key=lambda x: -(after["labels"].get(x) or 0))
    out.append("")
    out.append("== 标签体系 ==")
    sc_b = int(before.get("standard_count") or 0)
    sc_a = int(after.get("standard_count") or 0)
    sc_delta = sc_a - sc_b
    out.append(
        "  标准标签  %d → %d%s" % (sc_b, sc_a, f"   ({sc_delta:+d})" if sc_delta else "")
    )
    gone_line = f"  消失 {len(gone)} 个"
    cleared_names = (after.get("plan") or before.get("plan") or {}).get(
        "cleared_labels"
    ) or []
    if cleared_names:
        head = " / ".join(cleared_names[:6]) + (" …" if len(cleared_names) > 6 else "")
        gone_line += f"(其中清空动作词 {len(cleared_names)} 个:{head})"
    if gone:
        gone_line += ":" + " / ".join(gone[:12]) + (" …" if len(gone) > 12 else "")
    out.append(gone_line)
    if new:
        out.append(
            f"  新增 {len(new)} 个"
            + (":" + " / ".join(new[:12]) + (" …" if len(new) > 12 else ""))
        )

    # ---- 可比指标 ----
    out.append("")
    out.append("== 指标对比 ==")
    header = (
        _pad("指标", 20)
        + _pad("前", 10, "right")
        + _pad("后", 10, "right")
        + _pad("变化", 11, "right")
        + "  判断"
    )
    out.append("  " + header)
    out.append("  " + "-" * (_dw(header) + 2))

    for key, name, kind in ROWS:
        d = _delta(before, after, key)
        if d is None:
            continue
        tone = _tone(key, d, before, after)
        is_rate = kind == "rate"
        b_txt = _pct(before.get(key)) if is_rate else str(before.get(key))
        a_txt = _pct(after.get(key)) if is_rate else str(after.get(key))
        d_txt = (
            "—" if abs(d) < 1e-9 else (_pp(d) if is_rate else f"{d:+.0f}")
        )
        line = (
            "  "
            + _pad(name, 20)
            + _pad(b_txt, 10, "right")
            + _pad(a_txt, 10, "right")
            + _pad(d_txt, 11, "right")
            + "  "
            + TONE_MARK.get(tone, tone)
        )
        note = _explain(key, tone, before, after, d)
        if note:
            line += "  " + note
        out.append(line)
        if tone == "bad":
            flags.append(name)

    # ---- 口径变化:分母变了,数字不能直接比 ----
    if _sc_changed(before, after):
        sc_b = int(before.get("standard_count") or 0)
        sc_a = int(after.get("standard_count") or 0)
        out.append("")
        out.append("== 口径变化(不能直接比) ==")
        out.append(
            f"  标准标签覆盖率  {_pct(before.get('standard_rate'))} → "
            f"{_pct(after.get('standard_rate'))}   分母 {sc_b} → {sc_a}"
        )
        if sc_a < sc_b:
            out.append(
                "  分母变小,即使一个标签都没多用,覆盖率也会自动上升 —— "
                "它不能用来证明「分类变准了」。"
            )
        else:
            out.append(
                "  分母变大,新标签还没被用上,覆盖率会天然下降 —— "
                "不代表判别变差了。"
            )
        out.append(
            "  归并率 / 待归并碎片同理:标签合并会让同一标签下聚集更多组,"
            "(群, 标签) 收敛到单组的比例自然下降,不能当退化。"
        )

    # ---- 不可比 ----
    out.append("")
    out.append("== 不可比(已排除) ==")
    out.append(
        "  归一化收敛率 / 召回窄化率 / 缝合确认率 是「进程内累计」,重启归零;"
        "两次快照之间大概率重启过(部署),差值没有意义。"
    )
    out.append("  要看它们的变化,只能在同一进程内连续取样。")

    # ---- 结论 ----
    out.append("")
    out.append("== 结论 ==")
    idx = 0
    if _sc_changed(before, after):
        idx += 1
        out.append(
            f"  {idx}. 标签体系已变更(标准 {before.get('standard_count')} → "
            f"{after.get('standard_count')}),前后口径不同,跨时点的横向报表要标注日期。"
        )
    if gone:
        idx += 1
        out.append(
            f"  {idx}. {len(gone)} 个旧标签不再出现(合并改名或清空),"
            "它们的历史组已被改写;动作词那部分的组回到未分类队列。"
        )
    off_b = int(before.get("off_standard_count") or 0)
    off_a = int(after.get("off_standard_count") or 0)
    if off_a < off_b:
        idx += 1
        out.append(
            f"  {idx}. 非标准标签漏网 {off_b} → {off_a}:旧名进了别名表,"
            "以后 LLM 再输出这些名字会自动收敛,不会长回来。"
        )
    expect_n = sum(
        1
        for key, name, kind in ROWS
        if (d := _delta(before, after, key)) is not None
        and _tone(key, d, before, after) == "expect"
    )
    if expect_n:
        idx += 1
        out.append(
            f"  {idx}. {expect_n} 项是「预期变化」(动作词回队列),不是退化 —— "
            "等人工定标之后,未分类率才会回落。"
        )
    if flags:
        idx += 1
        out.append(
            f"  {idx}. 有 {len(flags)} 项真正变差:{'、'.join(flags)}。"
            "这几项跟体系改造无关,需要单独排查。"
        )
    if not idx:
        out.append("  1. 指标基本没变 —— 若体系确实改了,说明改动只落在小部分组上。")

    pend = int(after.get("pending_groups") or 0)
    if pend:
        out.append("")
        out.append(f"  下一步:{pend} 组在复核队列里,去 /review 定标(未分类优先)。")
    return "\n".join(out)


def render_summary(data: dict) -> str:
    out = [
        f"== 当前指标({data.get('created_at')}) ==",
        f"  标准标签 {data['standard_count']} 个 / 库内用过 {data['topic_count']} 个"
        f" / 组 {data['groups']} 个",
        f"  未分类 {data['unclassified_groups']} 组({_pct(data['unclassified_rate'])})"
        f" / 非标准漏网 {data['off_standard_count']} 个",
        f"  覆盖率 {_pct(data['standard_rate'])} / 低频占比 {_pct(data['low_frequency_ratio'])}"
        f" / 归并率 {_pct(data['merge_rate'])}",
        f"  待复核 {data['pending_groups']} 组 / gold set {data['gold_groups']} 组",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(
        description="标签体系改造前后的指标快照与对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/metrics_snapshot.py --list\n"
            "  python scripts/metrics_snapshot.py --save after\n"
            "  python scripts/metrics_snapshot.py --diff data/metrics-before-x.json "
            "data/metrics-after-y.json\n"
        ),
    )
    parser.add_argument("--save", metavar="LABEL", default="", help="抓一份快照并落盘(如 before / after)")
    parser.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"), help="对比两份快照")
    parser.add_argument("--list", action="store_true", help="列出现有快照")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="快照目录(默认 data/)")
    args = parser.parse_args()

    out_dir = Path(args.dir)

    if args.list:
        files = sorted(glob.glob(str(out_dir / "metrics-*.json")))
        if not files:
            print(f"还没有快照({out_dir}/metrics-*.json)。")
            print("先跑:python scripts/metrics_snapshot.py --save before")
            return 0
        print(f"{'文件':<52}{'标签':<12}{'时间':<20}标准/库内/组")
        for f in files:
            try:
                d = load(Path(f))
            except (ValueError, json.JSONDecodeError) as e:
                print(f"{Path(f).name:<52}(读取失败:{e})")
                continue
            print(
                _pad(Path(f).name, 52)
                + _pad(d.get("label") or "-", 12)
                + _pad(str(d.get("created_at") or "")[:19].replace("T", " "), 20)
                + f"{d.get('standard_count')}/{d.get('topic_count')}/{d.get('groups')}"
            )
        return 0

    if args.diff:
        before = load(Path(args.diff[0]))
        after = load(Path(args.diff[1]))
        print(render_diff(before, after))
        return 0

    from app.core.db import SessionLocal

    with SessionLocal() as session:
        if args.save:
            path = save(session, args.save, out_dir)
            print(f"已保存快照:{path}")
            print(render_summary(load(path)))
            return 0
        print(render_summary(capture(session)))
        print("\n(未落盘。要存下来加 --save <标签>,要对比加 --diff <前> <后>)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
