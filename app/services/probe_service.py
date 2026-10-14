"""群聊数据探查服务。

用途:为「话题段落重新归类(方案Y:整群话题段)」提供真实数据依据——
消息量级、时间分布、类型/发言者分布、相邻消息间隔分位数(决定段切分阈值)、
当前归类状态,并抽取最新消息原文帮助观察群内表达方式。

与一次性脚本的区别:
- 核心逻辑封装为 ProbeService(结构化结果 dict),可复用于:
  * CLI(scripts/probe_data.py)
  * 未来的 admin/probe HTTP 接口
  * 方案Y重算后的效果对比验证
- 数据访问走项目统一 SessionLocal / ORM,不改动数据库(只读探查)。
"""
from __future__ import annotations

import datetime
from collections import Counter
from typing import Optional

from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models.conversation import Conversation, MessageLink
from app.models.message import Message

# 探查抽样时的默认消息条数上限
_DEFAULT_LIMIT = 2000
# 报告间隔分布时关注的秒级阈值点(秒)。900=15min,用于评估"时间硬切"阈值
_GAP_HARD_CUT_SEC = 900


def _fmt_time(create_time: str) -> str:
    """毫秒时间戳字符串 -> 可读时间;失败原样返回。"""
    try:
        ts = int(create_time)
        if ts > 10**12:  # 毫秒 -> 秒
            ts /= 1000
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return create_time or "-"


def _to_sec(create_time: str) -> Optional[int]:
    """毫秒时间戳字符串 -> 绝对秒(用于间隔计算);失败返回 None。"""
    try:
        ts = int(create_time)
        if ts > 10**12:
            ts /= 1000
        return int(ts)
    except (TypeError, ValueError):
        return None


class ProbeService:
    """群聊数据只读探查。所有方法均不写库。"""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory

    # ------------------------------------------------------------ 入口
    def probe(self, chat_id: Optional[str] = None, limit: int = _DEFAULT_LIMIT) -> dict:
        """执行一整套探查,返回结构化结果 dict。

        - chat_id: 不传则统计全部群,抽样按全库时间正序
        - limit:   取多少条消息做样本分析(默认 2000)
        """
        with self._session_factory() as session:
            report: dict = {}
            report["总览"] = self._overview(session)
            report["各群消息数TOP"] = self._chats_top(session)
            rows = self._sample(session, chat_id, limit)
            report["抽样"] = self._sample_stats(rows)
            report["当前归类状态"] = self._grouping_state(session)
            report["最近消息样本"] = self._recent_sample(rows)
            return report

    # ------------------------------------------------------------ 各模块
    def _overview(self, session) -> dict:
        total = session.execute(select(func.count(Message.id))).scalar_one()
        return {"消息总量": total}

    def _chats_top(self, session, top: int = 10) -> list[dict]:
        rows = (
            session.execute(
                select(Message.chat_id, func.count(Message.id).label("cnt"))
                .group_by(Message.chat_id)
                .order_by(func.count(Message.id).desc())
                .limit(top)
            )
            .all()
        )
        return [{"chat_id": cid, "消息数": cnt} for cid, cnt in rows]

    def _sample(self, session, chat_id: Optional[str], limit: int):
        q = select(Message)
        if chat_id:
            q = q.where(Message.chat_id == chat_id)
        # 注意:create_time 是字符串时间戳,用 received_at(时间)排序更稳
        q = q.order_by(Message.received_at.asc(), Message.id.asc()).limit(limit)
        return list(session.scalars(q).all())

    def _sample_stats(self, rows: list) -> dict:
        n = len(rows)
        info: dict = {"条数": n}
        if not rows:
            return info

        # 时间跨度
        t0_raw, t1_raw = rows[0].create_time, rows[-1].create_time
        t0, t1 = _to_sec(t0_raw), _to_sec(t1_raw)
        info["最早"] = _fmt_time(t0_raw)
        info["最晚"] = _fmt_time(t1_raw)
        if t0 is not None and t1 is not None:
            span_min = (t1 - t0) / 60.0
            info["跨度分钟"] = round(span_min)
            info["跨度小时"] = round(span_min / 60.0, 1)

        # 类型分布
        info["类型分布"] = dict(Counter(m.message_type for m in rows).most_common())

        # 发言者分布
        senders = Counter(m.sender_id for m in rows if m.sender_id)
        info["发言者数"] = len(senders)
        info["发言者TOP"] = [
            {"sender_id": sid, "消息数": c} for sid, c in senders.most_common(8)
        ]

        # 文本长度分布
        lens = [len((m.text or "").strip()) for m in rows if (m.text or "").strip()]
        info["文本消息数"] = len(lens)
        if lens:
            info["平均长度"] = round(sum(lens) / len(lens))
            short = sum(1 for L in lens if L <= 20)
            info["短消息数(<=20字)"] = short
            info["短消息占比%"] = short * 100 // len(lens)

        # 相邻消息间隔分位数(秒)
        gaps, prev = [], None
        for m in rows:
            cur = _to_sec(m.create_time)
            if prev is not None and cur is not None:
                gaps.append(cur - prev)
            prev = cur
        info["间隔样本数"] = len(gaps)
        if gaps:
            gaps.sort()
            info["间隔分位数秒"] = {
                name: gaps[int(p * (len(gaps) - 1))]
                for name, p in [
                    ("P50", 0.50),
                    ("P80", 0.80),
                    ("P90", 0.90),
                    ("P95", 0.95),
                    ("P99", 0.99),
                ]
            }
            big = sum(1 for g in gaps if g > _GAP_HARD_CUT_SEC)
            info[f"间隔>{_GAP_HARD_CUT_SEC}秒段点数"] = big
        return info

    def _grouping_state(self, session) -> dict:
        roots = session.execute(
            select(func.count(func.distinct(Message.root_message_id)))
        ).scalar_one()
        convs = session.execute(select(func.count(Conversation.id))).scalar_one()
        links = session.execute(select(func.count(MessageLink.id))).scalar_one()
        with_topic = session.execute(
            select(func.count(Message.id)).where(Message.topic != "")
        ).scalar_one()
        return {
            "独立root_message_id": roots,
            "conversations表记录": convs,
            "message_links回复边": links,
            "已打topic标签消息": with_topic,
        }

    def _recent_sample(self, rows: list, n: int = 20) -> list[dict]:
        items = []
        for m in rows[-n:]:
            items.append(
                {
                    "时间": _fmt_time(m.create_time),
                    "sender": m.sender_id,
                    "type": m.message_type,
                    "text": (m.text or "").strip().replace("\n", " ")[:40],
                    "回复了": (m.parent_message_id or "")[:12],
                }
            )
        return items

    # ------------------------------------------------------------ 文本报告
    def to_text(self, report: dict) -> str:
        """把结构化 probe 结果渲染成控制台可读文本(CLI 打印用)。"""
        lines: list[str] = []

        ov = report.get("总览", {})
        lines.append(f"=== 消息总量: {ov.get('消息总量', 0)} ===")

        chats = report.get("各群消息数TOP", [])
        lines.append("\n=== 各群消息数 TOP ===")
        for c in chats:
            lines.append(f"  {c.get('chat_id')}: {c.get('消息数')} 条")

        smp = report.get("抽样", {})
        if smp.get("条数"):
            lines.append(f"\n=== 抽样消息 {smp['条数']} 条(时间正序) ===")
            lines.append(f"  最早: {smp.get('最早')}  最晚: {smp.get('最晚')}")
            if "跨度分钟" in smp:
                lines.append(
                    f"  跨度: {smp['跨度分钟']} 分钟 (约 {smp.get('跨度小时')} 小时)"
                )

            lines.append("\n=== 消息类型分布 ===")
            for t, c in (smp.get("类型分布") or {}).items():
                lines.append(f"  {t}: {c}")

            lines.append(f"\n=== 发言者数(样本内): {smp.get('发言者数', 0)} ===")
            for s in smp.get("发言者TOP", []):
                lines.append(f"  {s.get('sender_id')}: {s.get('消息数')} 条")

            tl = smp.get("文本消息数", 0)
            if tl:
                lines.append(
                    f"\n=== 文本消息 {tl} 条,平均长度 {smp.get('平均长度', 0)} 字 ==="
                )
                lines.append(
                    f"  短消息(<=20字): {smp.get('短消息数(<=20字)', 0)} 条 "
                    f"({smp.get('短消息占比%', 0)}%)"
                )

            gaps_n = smp.get("间隔样本数", 0)
            if gaps_n:
                lines.append(f"\n=== 相邻消息间隔(秒) 分位数 (样本 {gaps_n}) ===")
                for name, val in (smp.get("间隔分位数秒") or {}).items():
                    lines.append(f"  {name}: {val}s")
                lines.append(
                    f"  间隔>{_GAP_HARD_CUT_SEC}秒 的断点数: "
                    f"{smp.get(f'间隔>{_GAP_HARD_CUT_SEC}秒段点数', 0)}"
                )

        gs = report.get("当前归类状态", {})
        lines.append("\n=== 当前归类状态 ===")
        for k, v in gs.items():
            lines.append(f"  {k}: {v}")

        recent = report.get("最近消息样本", [])
        lines.append(f"\n=== 最近 {len(recent)} 条消息原文(看表达方式) ===")
        for item in recent:
            prefix = f"  [{item.get('时间')}] {str(item.get('sender'))[:12]:12s} ({item.get('type')}) {item.get('text')}"
            lines.append(prefix)
            if item.get("回复了"):
                lines.append(f"      ↳ 回复了 {item['回复了']}")

        lines.append("\n完成。")
        return "\n".join(lines)
