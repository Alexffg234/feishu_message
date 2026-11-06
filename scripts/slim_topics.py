#!/usr/bin/env python3
"""标准标签体系瘦身:把「组名当分类 + 组内混动作词」压平成「大类 > 现象」两层。

为什么必须改
------------
现状(41 个标准标签散在 18 个组里)不是「标签不够多」,而是**体系结构在制造噪声**:

1. **层级语义是反的**。子组名「黑屏类问题」在充当分类,组内的 topics 才是具体
   现象(酷喵黑屏 / 切换4K黑屏 / 有声无图)。结果父组「画质类问题」topics 为空,
   永远不出现在闭集里,白占一层;LLM 看到的却是
   「画质类问题 > 黑屏类问题: 酷喵黑屏 / 切换4K黑屏」。这一层不提供任何信息,
   只增加选择成本。
2. **一个组里混了不同粒度的东西**。「画面模糊类问题」下同时有现象(画面模糊)、
   动作(画质推送 / 画质模糊推送升级)和另一个现象(三方应用播放视频有黑点)。
   动作词进 topic 是硬伤:topic 回答「用户遇到什么问题」,stage 回答「处理到
   哪一步」。动作词放 topic,既污染主题统计,又让「已推」「上门维修」这类标签
   永远无法被正确归类。
3. **同义没有收敛**。「投屏卡顿 / 镜像投屏卡顿」「网络不通 / 网络连接问题」
   「画面模糊 / 画质模糊」各自都是一个标签,导致 82% 的标签只用过一次——
   不是分类太细,是同一类现象被拆成了多个名字。

改法
----
- 只留两层:**大类(稳定问题域) > 现象(统一粒度,不带「类问题」后缀)**。
- 动作词从 topic 摘出,回未分类队列(它的位置在 stage)。
- 同义收敛成一个现象名,旧名写进 `topic_aliases`(auto=0),以后 LLM 再输出旧名
  会被 `normalize_label` 自动收敛——瘦身不只是删标签,还要保证以后不会再长回来。

用法
----
    python scripts/slim_topics.py                 # 预览(默认,不写库)
    python scripts/slim_topics.py --apply         # 写库(先落备份)
    python scripts/slim_topics.py --restore data/topic_slim_backup-xxx.json
    python scripts/slim_topics.py --apply --force # 生产库缺 stage 列时强行写(不推荐)

安全性
------
- 默认 dry-run;`--apply` 写库前把**每条消息的原始 topic** 快照到
  `data/topic_slim_backup-<ts>.json`,`--restore` 逐条还原(合并是 N→1 的,
  没有快照就再也分不开)。
- `--apply` 还会自动抓**前后两份指标快照**(`data/metrics-before-*.json` /
  `metrics-after-*.json`)并打印对比。「前」只能在写库那一刻抓:apply 之后
  `messages.topic` 已改写,库里的「前」状态不复存在。抓取失败不阻断写库,
  但会明确报出来。
- 生产库若缺 `stage` 列(说明代码没部署或没重启),**拒绝 --apply**:此时摘掉
  「已推」「上门维修」会连唯一的进度信号一起丢掉。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402

# ---------------------------------------------------------------- 瘦身方案
# 目标体系:11 个大类 / 33 个现象。大类只作语境,闭集里只列现象。
NEW_GROUPS: list[tuple[str, list[str]]] = [
    ("画质类问题", ["画面模糊", "花屏", "闪屏", "黑屏", "画面黑点", "亮度异常"]),
    ("声音类问题", ["播放无声音", "声音异常"]),
    ("语音类问题", ["语音唤醒无反应", "语音识别异常", "语音网络错误"]),
    (
        "系统类问题",
        [
            "系统重启死机",
            "无法开机",
            "自动开机",
            "开机跳转应用",
            "系统升级异常",
            "无法输入密码",
            "恢复出厂设置",
            "远程删除软件",
        ],
    ),
    ("网络连接问题", ["网络不通", "网络断流"]),
    ("投屏问题", ["投屏找不到设备", "投屏卡顿"]),
    ("会员类问题", ["会员领取查询", "充值二维码无法扫描"]),
    ("三方APP类问题", ["第三方APP卡顿", "APP安装失败"]),
    ("外设配件类问题", ["麦克风无法连接", "移动硬盘无法读取"]),
    ("电视硬件类问题", ["屏脱胶", "硬件检测"]),
    ("功能设置类问题", ["关闭游戏板块", "关闭动漫板块推荐"]),
]

# 旧标签 -> 新标签。空串 = 清空回未分类(交人工)。
# 覆盖「当前标准标签」与「库里实际用过的非标准标签」两类,一个都不能漏:
# 漏掉的标准标签会连人带组一起消失(脚本启动即校验)。
MERGES: dict[str, str] = {
    # ---- 画质:6 个黑屏合 1,模糊族 4 合 1,其余各归位 ----
    "画面模糊": "画面模糊",
    "画质模糊": "画面模糊",
    "画质模糊推送升级": "画面模糊",   # 现象是模糊,「推送升级」是动作
    "花屏问题": "花屏",
    "播放U盘闪屏": "闪屏",            # 载体是 U 盘,现象仍是闪屏
    "酷喵黑屏": "黑屏",
    "切换4K黑屏": "黑屏",
    "开机黑屏有声音": "黑屏",
    "有声无图": "黑屏",
    "机顶盒不定时黑屏": "黑屏",
    "三方应用播放视频有黑点": "画面黑点",
    "亮度无法调节": "亮度异常",
    # ---- 声音 ----
    "播放无声音": "播放无声音",
    "播放静音": "播放无声音",
    "音爆声": "声音异常",
    "音效": "声音异常",
    # ---- 语音:唤醒无反应 4 合 1 ----
    "语音唤醒不识别": "语音唤醒无反应",
    "远场语音没反应": "语音唤醒无反应",
    "远讲问题": "语音唤醒无反应",
    "语音不好用": "语音唤醒无反应",
    "语音识别无跳转": "语音识别异常",
    "语音网络错误": "语音网络错误",
    # ---- 系统 ----
    "系统不定时重启": "系统重启死机",
    "联通直播死机": "系统重启死机",
    "网络电视闪退": "系统重启死机",
    "开机进入主页崩溃": "系统重启死机",
    "新机无法进入主界面": "无法开机",
    "自动开机": "自动开机",
    "开机跳转应用": "开机跳转应用",
    "取消推送更新": "系统升级异常",   # 用户不想被升级,本质是升级策略问题
    "无法输入密码": "无法输入密码",
    "恢复出厂设置": "恢复出厂设置",
    "远程删除软件": "远程删除软件",
    # ---- 网络:组名复读机合 1 ----
    "网络不通": "网络不通",
    "网络连接问题": "网络不通",
    "网络断流": "网络断流",
    # ---- 投屏 ----
    "投屏找不到设备": "投屏找不到设备",
    "投屏卡顿": "投屏卡顿",
    "镜像投屏卡顿": "投屏卡顿",       # 镜像只是投屏方式,不是另一种故障
    # ---- 会员 ----
    "会员领取查询": "会员领取查询",
    "会员查询": "会员领取查询",
    "住居美学馆充值二维码无法扫描": "充值二维码无法扫描",
    # ---- 三方 APP ----
    "第三方APP卡顿": "第三方APP卡顿",
    "AI学习APP安装": "APP安装失败",
    # ---- 外设 ----
    "海尔商城购买的麦克风无法连接电视": "麦克风无法连接",
    "移动硬盘无法读取": "移动硬盘无法读取",
    # ---- 硬件 ----
    "屏脱胶": "屏脱胶",
    "硬件检测": "硬件检测",
    # ---- 功能设置 ----
    "关闭游戏板块": "关闭游戏板块",
    "屏蔽游戏tab": "关闭游戏板块",
    "关闭动漫板块推荐": "关闭动漫板块推荐",
    # ---- 清空:动作词与噪声,回未分类队列由人判定 ----
    "换机推送": "",          # 动作/结论
    "画质推送": "",          # 动作
    "已推": "",              # stage 词
    "上门维修": "",          # stage 词
    "屏幕": "",              # 太笼统,不是现象
    "测试": "",              # 噪声
    "视频": "",              # 噪声
    "好的": "",              # 噪声
    "PPT美化": "",           # 噪声
    "商场演示机": "",         # 不是故障
    "投屏问题自查手册": "",    # 文档标题,不是现象
    "播放异常排查": "",       # 动作
    "海尔智家绑定失败": "",    # 现有 11 类里没有对应现象,交人工决定要不要新增
}


# ---------------------------------------------------------------- 方案校验
def validate_plan() -> dict:
    """把 NEW_GROUPS/MERGES 归一化并校验不变量(启动即跑,防止手改方案埋雷)。

    返回 {"standards":[...], "group_of":{label: 大类}, "aliases":{old: new}}。
    """
    standards: list[str] = []
    group_of: dict[str, str] = {}
    for group, topics in NEW_GROUPS:
        for t in topics:
            t = (t or "").strip()
            if not t:
                raise ValueError(f"大类 {group!r} 里有空标签")
            if t in group_of:
                raise ValueError(f"标签 {t!r} 同时出现在 {group_of[t]!r} 与 {group!r}")
            if t.endswith(("类问题", "的问题", "的情况")):
                raise ValueError(f"标签 {t!r} 带无信息后缀,现象名不应带「类问题」")
            group_of[t] = group
            standards.append(t)

    aliases: dict[str, str] = {}
    for old, new in MERGES.items():
        old = (old or "").strip()
        new = (new or "").strip()
        if not old:
            raise ValueError("MERGES 里有空键")
        if new and new not in group_of:
            raise ValueError(f"{old!r} 的目标 {new!r} 不在新标准集里")
        if old == new:
            continue                      # 原名保留,不需要别名
        if new:
            aliases[old] = new
    return {"standards": standards, "group_of": group_of, "aliases": aliases}


# ---------------------------------------------------------------- 现状采集
def collect_state(session) -> dict:
    """当前体系 + 库内实际使用情况。"""
    groups = session.execute(
        text("SELECT name, parent, topics, sort_order FROM topic_groups ORDER BY sort_order, id")
    ).all()
    cur_groups = [
        {
            "name": r[0],
            "parent": r[1] or "",
            "topics": _as_topics(r[2]),
            "sort_order": int(r[3] or 0),
        }
        for r in groups
    ]

    # 组数按 (chat,root) 去重后计数;不用 CONCAT('|') 拼接——MySQL 把 || 当逻辑或,
    # 而 CONCAT 在 sqlite 上又不存在,子查询写法两边都能跑
    used = session.execute(
        text(
            "SELECT topic, COUNT(*) AS g FROM ("
            "  SELECT chat_id, root_message_id, topic FROM messages"
            "  WHERE topic <> '' GROUP BY chat_id, root_message_id, topic"
            ") x GROUP BY topic ORDER BY g DESC"
        )
    ).all()
    counts = dict(
        session.execute(
            text(
                "SELECT topic, COUNT(*) FROM messages WHERE topic <> '' GROUP BY topic"
            )
        ).all()
    )
    used_topics = {
        r[0]: {"groups": int(r[1]), "msgs": int(counts.get(r[0], 0))} for r in used
    }

    total_msgs = session.execute(text("SELECT COUNT(*) FROM messages")).scalar() or 0
    total_groups = session.execute(
        text(
            "SELECT COUNT(*) FROM (SELECT chat_id, root_message_id FROM messages "
            "WHERE root_message_id <> '' GROUP BY chat_id, root_message_id) x"
        )
    ).scalar() or 0

    return {
        "groups": cur_groups,
        "used": used_topics,
        "total_msgs": int(total_msgs),
        "total_groups": int(total_groups),
        "standards": _current_standards(cur_groups),
        "conflicts": _knowledge_conflicts(session),
    }


def _as_topics(value) -> list[str]:
    """原生 SQL 取到的 JSONText 是字符串,ORM 取到的是 list,统一成 list。

    不处理会出现「151 个标准标签」这种假象:字符串被逐字符迭代了。
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(t).strip() for t in value if str(t or "").strip()]


def _current_standards(groups: list[dict]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for g in groups:
        for t in g["topics"]:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _knowledge_conflicts(session) -> list[dict]:
    """合并会让两条知识库记录撞到同一个 topic(topic 唯一)。

    不擅自删用户写的手册内容:只报出来,由人决定合并还是丢弃。
    """
    try:
        rows = session.execute(text("SELECT topic FROM topic_knowledge")).all()
    except Exception:  # noqa: BLE001 - 老库可能没有该表
        return []
    targets: dict[str, list[str]] = {}
    for (t,) in rows:
        new = MERGES.get(t, t)
        if new:
            targets.setdefault(new, []).append(t)
    return [
        {"target": k, "sources": v}
        for k, v in targets.items()
        if len(v) > 1
    ]


def preflight(bind=None) -> tuple[list[str], list[str]]:
    """返回 (warnings, blockers)。blockers 非空时 --apply 必须 --force。

    bind 可传 session.get_bind(),便于测试在 sqlite 上验证「缺列即阻断」。
    """
    bind = bind if bind is not None else engine
    cols = {c["name"] for c in inspect(bind).get_columns("messages")}
    warnings: list[str] = []
    blockers: list[str] = []
    if "stage" not in cols:
        blockers.append(
            "生产库 messages 缺 stage 列:说明代码没部署或没重启(迁移在 init_db 里跑)。"
            "此时摘掉「已推」「上门维修」会连唯一的进度信号一起丢掉。"
        )
    for c in ("topic_conf", "topic_source"):
        if c not in cols:
            warnings.append(f"生产库 messages 缺 {c} 列(同一原因),复核闭环暂时不可用。")
    return warnings, blockers


# ---------------------------------------------------------------- 报表
def summarize_plan(state: dict, plan: dict | None = None) -> dict:
    """方案摘要,塞进指标快照,让「前后对比」能解释未分类为什么上升。

    只靠两份指标快照是**无法**把「标签减少 8 个」拆成「合并几个 / 清空几个」的:
    合并掉的标签组换了名字,清空的标签组回到未分类,两种在聚合数字上都是
    「标准标签变少 + 未分类变多」。所以把方案自己的统计一起存下来。
    """
    used = state.get("used") or {}
    merged = kept = cleared = 0
    cleared_labels: list[str] = []
    cleared_groups = 0
    for old in MERGES:
        new = (MERGES[old] or "").strip()
        info = used.get(old) or {"groups": 0, "msgs": 0}
        if not new:
            cleared += 1
            if info["groups"]:
                cleared_labels.append(old)
                cleared_groups += info["groups"]
        elif old == new:
            kept += 1
        else:
            merged += 1
    return {
        "merged": merged,
        "kept": kept,
        "cleared": cleared,
        "cleared_labels": cleared_labels,
        "cleared_groups": cleared_groups,
        "target_standards": len((plan or {}).get("standards") or []) or None,
    }


def render_report(state: dict, plan: dict) -> str:
    standards, group_of = plan["standards"], plan["group_of"]
    used = state["used"]
    lines: list[str] = []

    old_std = state["standards"]
    sub_groups = [g for g in state["groups"] if g["parent"]]
    lines.append("== 当前 ==")
    lines.append(
        f"大组 {len(state['groups'])} 个(其中子组 {len(sub_groups)} 个,父组 topics 多为空)"
        f" / 标准标签 {len(old_std)} 个 / 库内用过 {len(used)} 个"
    )
    lines.append(
        f"消息 {state['total_msgs']} 条 / 组 {state['total_groups']} 个"
    )

    lines.append("")
    lines.append("== 目标 ==")
    lines.append(f"大类 {len(NEW_GROUPS)} 个(压平为两层,无子组)/ 现象 {len(standards)} 个")

    lines.append("")
    lines.append("== 标签去向 ==")
    lines.append(f"{'旧标签':<28}{'新标签':<18}{'影响':>12}  备注")
    lines.append("-" * 78)
    kept = merged = cleared = untouched = 0
    for old in sorted(MERGES, key=lambda x: (-used.get(x, {}).get("msgs", 0), x)):
        new = (MERGES[old] or "").strip()
        info = used.get(old, {"groups": 0, "msgs": 0})
        impact = f"{info['groups']} 组 {info['msgs']} 条" if info["msgs"] else "0 条(未用)"
        if not new:
            note = "清空 -> 未分类队列"
            cleared += 1
        elif new == old:
            note = "保持"
            kept += 1
        else:
            note = ""
            merged += 1
        lines.append(f"{old:<28}{new or '(清空)':<18}{impact:>12}  {note}")

    # 库里用过但方案没覆盖的标签:必须为 0,否则会留下没人管的孤儿
    missing = [t for t in used if t not in MERGES]
    lines.append("")
    lines.append("== 汇总 ==")
    lines.append(f"标准标签 {len(old_std)} -> {len(standards)}({len(standards) - len(old_std):+d})")
    lines.append(
        f"库内标签 {len(used)} -> {len(standards)}"
        f"(合并 {merged} / 保持 {kept} / 清空 {cleared})"
    )
    lines.append(
        f"新写别名 {len(plan['aliases'])} 条(auto=0,以后 LLM 再输出旧名会自动收敛)"
    )
    if missing:
        lines.append("")
        lines.append(f"!! 有 {len(missing)} 个库内标签没写进 MERGES,会变成孤儿:{missing}")

    lines.append("")
    lines.append("== 需要人工接手 ==")
    cleared_labels = [o for o, n in MERGES.items() if not (n or "").strip() and o in used]
    lines.append(
        f"清空的 {len(cleared_labels)} 个标签对应的组会回到未分类队列,在 /review 里逐条定标:"
    )
    for t in cleared_labels:
        info = used[t]
        lines.append(f"  {t}({info['groups']} 组 {info['msgs']} 条)")

    if state["conflicts"]:
        lines.append("")
        lines.append("== 知识库冲突(不会自动删,需人工决定) ==")
        for c in state["conflicts"]:
            lines.append(f"  {c['target']} <- {' / '.join(c['sources'])}(topic 唯一,只能留一条)")

    return "\n".join(lines)


# ---------------------------------------------------------------- 指标快照
def _try_capture(session, label: str, backup_dir: Path, summary: dict):
    """抓一份指标快照(best-effort),失败不阻断写库。

    为什么不阻断:apply 的主职责是改体系;抓不到基线会让「前后对比」不可用,
    但比「用户已经确认过方案、却被一个统计接口挡回去」要好。失败时明确报出来,
    别让人以为对比没做是因为没差异。
    """
    try:
        from metrics_snapshot import save as save_metrics

        return save_metrics(session, label, backup_dir, plan=summary)
    except Exception as e:  # noqa: BLE001 —— 快照失败原因很多(缺列/服务未就绪),一律降级
        print(f"  ! 无法抓取 {label} 指标快照:{e}")
        print("    (不影响写库;但这次改造的前后对比不可用)")
        return None


# ---------------------------------------------------------------- 落库
def _snapshot(session, plan: dict, backup_dir: Path, extra: dict | None = None) -> Path:
    """把逐条原始 topic 存盘。合并是 N→1,没有快照就再也分不开。"""
    msgs = session.execute(
        text("SELECT id, topic FROM messages WHERE topic <> ''")
    ).all()
    convs = session.execute(
        text("SELECT id, topic FROM conversations WHERE topic <> ''")
    ).all()
    groups = session.execute(
        text("SELECT name, parent, topics, sort_order FROM topic_groups ORDER BY sort_order, id")
    ).all()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"topic_slim_backup-{stamp}.json"
    payload = {
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "mapping": plan["aliases"],
        "cleared": [k for k, v in MERGES.items() if not (v or "").strip()],
        "new_groups": [{"name": n, "topics": t} for n, t in NEW_GROUPS],
        "old_groups": [
            {"name": r[0], "parent": r[1] or "", "topics": _as_topics(r[2]), "sort_order": int(r[3] or 0)}
            for r in groups
        ],
        "messages": {str(r[0]): r[1] for r in msgs},
        "conversations": {str(r[0]): r[1] for r in convs},
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def apply_plan(session, plan: dict, backup_dir: Path, state: dict | None = None) -> dict:
    """写库:替换大组 -> 回刷 messages/conversations -> 写别名。"""
    state = state or collect_state(session)
    summary = summarize_plan(state, plan)

    # 「前」的指标只能在这里抓:apply 之后 messages.topic 已被改写,库里的
    # 「前」状态不复存在(只剩 backup 里的原始 topic,要重算就得整库回滚)。
    metrics_before = _try_capture(session, "before", Path(backup_dir), summary)

    backup_path = _snapshot(
        session,
        plan,
        Path(backup_dir),
        extra={"metrics_before": str(metrics_before) if metrics_before else ""},
    )

    # 1) 大组整体替换(parent 一律清空 = 压平为两层)
    # created_at 是 Python 侧默认值,原生 INSERT 不会触发,必须显式给
    now = datetime.datetime.now()
    session.execute(text("DELETE FROM topic_groups"))
    for i, (name, topics) in enumerate(NEW_GROUPS):
        session.execute(
            text(
                "INSERT INTO topic_groups (name, parent, topics, sort_order, created_at) "
                "VALUES (:n, '', :t, :s, :c)"
            ),
            {
                "n": name,
                "t": json.dumps(topics, ensure_ascii=False),
                "s": i + 1,
                "c": now,
            },
        )

    # 2) 回刷存量(topic_conf 缺列时不碰它)
    # 注意:必须用 session.connection() 而不是 session.get_bind()。
    # inspect(engine) 会另签一条连接并归还,内存 SQLite(SingletonThreadPool)与
    # 当前事务共用同一连接,归还时的 rollback 会把上面刚做的 DELETE/INSERT 一起回滚。
    cols = {c["name"] for c in inspect(session.connection()).get_columns("messages")}
    has_conf = "topic_conf" in cols
    moved_msgs = moved_conv = cleared_msgs = 0
    for old, new in MERGES.items():
        new = (new or "").strip()
        if old == new:
            continue
        if has_conf and not new:
            r = session.execute(
                text("UPDATE messages SET topic = '', topic_conf = 0 WHERE topic = :o"),
                {"o": old},
            )
            cleared_msgs += r.rowcount or 0
        else:
            r = session.execute(
                text("UPDATE messages SET topic = :n WHERE topic = :o"),
                {"n": new, "o": old},
            )
            moved_msgs += r.rowcount or 0
        c = session.execute(
            text("UPDATE conversations SET topic = :n WHERE topic = :o"),
            {"n": new, "o": old},
        )
        moved_conv += c.rowcount or 0

    # 3) 旧名 -> 新名 写别名(auto=0:这是人工定下的映射,不该被自动建议覆盖)
    written = 0
    for alias, canonical in plan["aliases"].items():
        exists = session.execute(
            text("SELECT canonical FROM topic_aliases WHERE alias = :a"), {"a": alias}
        ).scalar()
        if exists is None:
            session.execute(
                text(
                    "INSERT INTO topic_aliases (alias, canonical, auto, created_at) "
                    "VALUES (:a, :c, 0, :t)"
                ),
                {"a": alias, "c": canonical, "t": now},
            )
            written += 1

    session.commit()

    # 「后」的指标在提交之后抓 —— 立刻抓是对的:这一刻的未分类率就是改造的
    # 直接后果;过几天再抓,里面就混进了人工定标的进度,不再是「改造效果」。
    metrics_after = _try_capture(session, "after", Path(backup_dir), summary)

    return {
        "backup": str(backup_path),
        "moved_msgs": moved_msgs,
        "cleared_msgs": cleared_msgs,
        "moved_conv": moved_conv,
        "aliases": written,
        "metrics_before": str(metrics_before) if metrics_before else "",
        "metrics_after": str(metrics_after) if metrics_after else "",
    }


def restore(session, path: Path) -> dict:
    """按快照逐条还原(大组 + 每条消息/对话的原始 topic)。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.datetime.now()
    session.execute(text("DELETE FROM topic_groups"))
    for g in payload["old_groups"]:
        session.execute(
            text(
                "INSERT INTO topic_groups (name, parent, topics, sort_order, created_at) "
                "VALUES (:n, :p, :t, :s, :c)"
            ),
            {
                "n": g["name"],
                "p": g.get("parent") or "",
                "t": json.dumps(g.get("topics") or [], ensure_ascii=False),
                "s": int(g.get("sort_order") or 0),
                "c": now,
            },
        )

    restore_ts = payload.get("created_at", "")
    msgs = [{"i": int(k), "t": v} for k, v in payload["messages"].items()]
    if msgs:
        session.execute(
            text("UPDATE messages SET topic = :t WHERE id = :i"), msgs
        )
    convs = [{"i": int(k), "t": v} for k, v in payload["conversations"].items()]
    if convs:
        session.execute(
            text("UPDATE conversations SET topic = :t WHERE id = :i"), convs
        )

    # 还原本次建立的别名(只删与快照 mapping 一致的那些,不误伤人工维护的)
    removed = 0
    for alias, canonical in (payload.get("mapping") or {}).items():
        r = session.execute(
            text("DELETE FROM topic_aliases WHERE alias = :a AND canonical = :c AND auto = 0"),
            {"a": alias, "c": canonical},
        )
        removed += r.rowcount or 0

    session.commit()
    return {
        "groups": len(payload["old_groups"]),
        "messages": len(msgs),
        "conversations": len(convs),
        "aliases_removed": removed,
        "created_at": restore_ts,
    }


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(
        description="标准标签体系瘦身(默认预览,不写库)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="真正写库(先落备份)")
    parser.add_argument("--force", action="store_true", help="preflight 阻断时强行写")
    parser.add_argument("--restore", default="", help="按备份文件还原")
    parser.add_argument(
        "--backup-dir", default="data", help="备份目录(默认 data/)"
    )
    args = parser.parse_args()

    plan = validate_plan()

    with SessionLocal() as session:
        if args.restore:
            res = restore(session, Path(args.restore))
            print(f"已还原到 {res['created_at']} 的快照:")
            print(f"  大组 {res['groups']} 个 / 消息 {res['messages']} 条 / 对话 {res['conversations']} 条")
            print(f"  撤销本次写入的别名 {res['aliases_removed']} 条")
            return 0

        state = collect_state(session)
        print(render_report(state, plan))

        warnings, blockers = preflight(bind=session.connection())
        if warnings:
            print("\n== 警告 ==")
            for w in warnings:
                print("  ! " + w)
        if blockers:
            print("\n== 阻断 ==")
            for b in blockers:
                print("  X " + b)

        missing = [t for t in state["used"] if t not in MERGES]
        if missing:
            print("\n方案不完整:有库内标签没写进 MERGES,先补齐再动。")
            return 1

        if not args.apply:
            print("\n以上为预览,未修改任何数据;确认无误后加 --apply 执行")
            if blockers and not args.force:
                print("(注意:直接 --apply 会被上面的阻断拦下)")
            return 0

        if blockers and not args.force:
            print("\n已阻断,未改动数据。建议先部署最新代码并重启(迁移会自动补列),")
            print("再跑 scripts/backfill_stage.py 回填阶段,然后回来执行 --apply。")
            print("确定要现在写就加 --force(会丢掉「已推」「上门维修」的进度信号)。")
            return 2

        res = apply_plan(session, plan, Path(args.backup_dir), state=state)
        print("\n== 已写入 ==")
        print(f"  大组:替换为 {len(NEW_GROUPS)} 个(压平,无子组)")
        print(f"  消息:改写 {res['moved_msgs']} 条,清空 {res['cleared_msgs']} 条")
        print(f"  对话副本:改写 {res['moved_conv']} 条")
        print(f"  别名:新增 {res['aliases']} 条")
        print(f"  备份:{res['backup']}")
        print(f"  回滚:python scripts/slim_topics.py --restore {res['backup']}")

        if res["metrics_before"] and res["metrics_after"]:
            from metrics_snapshot import load, render_diff

            print("")
            print(render_diff(load(Path(res["metrics_before"])),
                              load(Path(res["metrics_after"]))))
            print("")
            print(f"  基线快照:{res['metrics_before']}")
            print(f"  改造后快照:{res['metrics_after']}")
            print("  (过几天想看人工定标的进展,再抓一份对比:"
                  "python scripts/metrics_snapshot.py --save later)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
