"""群聊对话习惯分析服务（只读）。

用途：从采集到的群聊消息里挖掘“用户的对话习惯”——
谁最活跃、爱发什么、什么时候发、爱发长文还是短句、
更爱回复别人还是在自说自话、主要聊哪些话题。

与一次性脚本的区别（对齐 ProbeService 的架构约定）：
- 核心分析逻辑封装为 HabitsService，返回结构化 dict，可复用于：
  * CLI（scripts/analyze_habits.py）
  * 可视化页面 HTTP 接口（/api/habits）
  * 未来任意消费分析的场合
- 数据访问走项目统一 SessionLocal / ORM，只读不改库。
- 发送者优先显示 user_names 缓存里的真实姓名，没缓存则显示 open_id 前缀。
"""
from __future__ import annotations

import datetime
from collections import Counter
from typing import Optional

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.message import Message
from app.models.user_name import UserName

# 东八区偏移（小时），贴合中国用户作息
_TZ_HOURS = 8
_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 文字类消息类型（图片/文件等不进入文本长度统计）
_TEXT_TYPES = {"text", "post"}


def _sec(create_time: str) -> Optional[int]:
    """毫秒时间戳字符串 -> 绝对秒；失败返回 None。"""
    try:
        ts = int(create_time)
        if ts > 10**12:
            ts //= 1000
        return ts
    except (TypeError, ValueError):
        return None


def _datetime_utc(create_time: str) -> Optional[datetime.datetime]:
    s = _sec(create_time)
    if s is None:
        return None
    return datetime.datetime.fromtimestamp(s, tz=datetime.timezone.utc)


def _load_names(session) -> dict[str, str]:
    """读 user_names 缓存：open_id -> 真实姓名。"""
    names: dict[str, str] = {}
    try:
        rows = session.execute(select(UserName.user_id, UserName.name)).all()
        for uid, name in rows:
            if name:
                names[uid] = name
    except Exception:  # noqa: BLE001 - 表未建/连接异常，降级为无名字
        pass
    return names


def _display_name(uid: str, names: dict[str, str], width: int = 10) -> str:
    if uid in names:
        return names[uid]
    return (uid[:width] or "?") if uid else "匿名"


def analyze(chat_id: Optional[str] = None, limit: int = 2000) -> dict:
    """执行对话习惯分析。返回结构化 dict（供页面 / CLI 消费）。"""
    with SessionLocal() as session:
        q = select(Message)
        if chat_id:
            q = q.where(Message.chat_id == chat_id)
        q = q.order_by(Message.received_at.asc(), Message.id.asc()).limit(limit)
        messages = list(session.scalars(q).all())
        names = _load_names(session)

    n = len(messages)
    out: dict = {"样本条数": n, "样本跨度小时": None, "类型分布": {}, "发言者数": 0,
                 "发言者TOP": [], "文本消息数": 0, "回复率%": 0, "小时分布": [], "周分布": []}
    if not messages:
        return out

    # 时间跨度（样本首末）
    valid = [t for t in (_sec(m.create_time) for m in messages) if t is not None]
    if valid:
        out["样本跨度小时"] = round((max(valid) - min(valid)) / 3600.0, 1)

    # ---- 1) 类型分布
    out["类型分布"] = dict(Counter(m.message_type or "?" for m in messages).most_common())

    # ---- 2) 发言者活跃度
    by_sender: Counter = Counter((m.sender_id or "") for m in messages)
    active = [(sid, c) for sid, c in by_sender.most_common() if sid]
    out["发言者数"] = len(active)
    out["发言者TOP"] = [
        {"姓名": _display_name(sid, names), "消息数": c, "占比%": round(c * 100 / n, 1)}
        for sid, c in active[:10]
    ]
    out["最活跃者占比%"] = round(active[0][1] * 100 / n, 1) if active else 0

    # ---- 3) 文字表达
    text_msgs = [
        m for m in messages
        if m.message_type in _TEXT_TYPES and (m.text or "").strip()
    ]
    lens = [len(m.text.strip()) for m in text_msgs]
    out["文本消息数"] = len(lens)
    if lens:
        out["文字均长"] = round(sum(lens) / len(lens), 1)
        out["文字最短短"] = min(lens)
        out["文字最长"] = max(lens)
        seg = {
            "极短(<=5字)": sum(1 for L in lens if L <= 5),
            "短(6-20字)": sum(1 for L in lens if 6 <= L <= 20),
            "中(21-60字)": sum(1 for L in lens if 21 <= L <= 60),
            "长(>60字)": sum(1 for L in lens if L > 60),
        }
        out["文字长度分布"] = {
            k: {"条数": v, "占比%": round(v * 100 / len(lens), 1)}
            for k, v in seg.items()
        }

    # ---- 4) 互动模式
    reply = sum(1 for m in messages if m.parent_message_id)
    out["回复他人条数"] = reply
    out["回复率%"] = round(reply * 100 / n, 1)
    out["自主发言%"] = round((n - reply) * 100 / n, 1)
    root_cnt: Counter = Counter((m.root_message_id or "") for m in messages)
    chain_lens = [c for c in root_cnt.values() if c > 0]
    out["回复链表"] = len(chain_lens)
    if chain_lens:
        out["单话题消息条数分布"] = {
            "2条以下": sum(1 for c in chain_lens if c <= 2),
            "3-5条": sum(1 for c in chain_lens if 3 <= c <= 5),
            "6条以上": sum(1 for c in chain_lens if c >= 6),
        }

    # ---- 5) 时间习惯（东八区）
    hours: Counter = Counter()
    wdays: Counter = Counter()
    for m in messages:
        dt = _datetime_utc(m.create_time)
        if dt is None:
            continue
        local = dt + datetime.timedelta(hours=_TZ_HOURS)
        hours[local.hour] += 1
        wdays[local.weekday()] += 1
    if hours:
        out["最活跃小时"] = f"{hours.most_common(1)[0][0]:02d}时"
        out["小时分布"] = [{"hour": h, "count": hours[h]} for h in range(24)]
        night = sum(c for h, c in hours.items() if h >= 21 or h < 6)
        out["夜间占比%"] = round(night * 100 / n, 1)
    if wdays:
        out["周分布"] = [
            {"day": _WEEKDAYS[w], "count": wdays[w]} for w in range(7)
        ]
    return out


def to_text(out: dict) -> str:
    """把结构化结果渲染成控制台可读文本（CLI 打印用）。"""
    lines: list[str] = []
    n = out.get("样本条数", 0)
    lines.append("===== 群聊对话习惯分析 =====")
    lines.append("样本消息: %d 条%s" % (
        n,
        ("，跨度约 %s 小时" % out["样本跨度小时"]) if out.get("样本跨度小时") else "",
    ))
    if not n:
        lines.append("（无数据可分析）")
        lines.append("===== 分析完成 =====")
        return "\n".join(lines)

    lines.append("\n【消息形态】")
    for t, c in (out.get("类型分布") or {}).items():
        lines.append("  %s: %d 条 (%.1f%%)" % (t, c, c * 100 / n))

    lines.append("\n【活跃成员】(共 %d 人发言)" % out.get("发言者数", 0))
    for p in out.get("发言者TOP", []):
        lines.append("  %s: %d 条 (%.1f%%)" % (p["姓名"], p["消息数"], p["占比%"]))
    if out.get("最活跃者占比%"):
        lines.append("  最活跃者占全体消息 %.1f%%" % out["最活跃者占比%"])

    tl = out.get("文本消息数", 0)
    lines.append("\n【文字表达】(文本 %d 条)" % tl)
    if tl:
        lines.append("  平均 %s 字/条，范围 %s~%s 字" % (
            out.get("文字均长"), out.get("文字最短短"), out.get("文字最长")))
        for k, v in (out.get("文字长度分布") or {}).items():
            lines.append("    %s: %d 条 (%.1f%%)" % (k, v["条数"], v["占比%"]))

    lines.append("\n【互动模式】")
    lines.append("  回复他人: %d 条，回复率 %.1f%%" % (out.get("回复他人条数", 0), out.get("回复率%", 0)))
    lines.append("  自主发言(非回复): %.1f%%" % out.get("自主发言%", 0))
    if out.get("回复链表"):
        lines.append("  话题/回复链 %d 个，其中:" % out["回复链表"])
        for k, v in (out.get("单话题消息条数分布") or {}).items():
            lines.append("    %s: %d 个" % (k, v))

    lines.append("\n【时间习惯】(按东八区)")
    if out.get("最活跃小时"):
        lines.append("  最活跃时段: %s" % out["最活跃小时"])
    if out.get("夜间占比%") is not None:
        lines.append("  夜间(21-06点)占比: %.1f%%" % out["夜间占比%"])
    top_hours = sorted((out.get("小时分布") or []), key=lambda x: -x["count"])[:5]
    if top_hours and top_hours[0]["count"]:
        lines.append("  活跃小时TOP:")
        for t in top_hours:
            bar = "#" * max(1, round(t["count"] / top_hours[0]["count"] * 20))
            lines.append("    %02d-%02d点: %d 条 %s" % (t["hour"], t["hour"] + 1, t["count"], bar))
    week = sorted((out.get("周分布") or []), key=lambda x: -x["count"])
    if week and week[0]["count"]:
        lines.append("  活跃星期TOP:")
        for d in week:
            lines.append("    %s: %d 条" % (d["day"], d["count"]))

    lines.append("\n===== 分析完成 =====")
    return "\n".join(lines)
