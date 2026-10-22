"""标签归并:用 LLM 判定同群内哪些话题标签属于同一问题,统一到保留标签。

同标签归组只认完全相同的标签;LLM 对同一故障案例的碎片组可能贴出
「亮度调节 / 读取开机方法 / 自动开机 / 本控按键」这类近义标签,导致
一个案例被拆成多个话题。本模块输入全部标签 + 每标签最近消息样本,
LLM 输出 {待合并标签: 保留标签} 映射,应用后触发同标签归并。

语义:
- 只处理同群(chat_id 内)的标签
- 保留标签必须是输入中已存在的标签,不得新造
- 保留标签为标准标签(问题大类配置的 topic 名)时只改名不合并
  (标准名被多个不同案例共享,合并会串案)
- 幂等:无映射或标签已统一时为无操作
- 开关:settings.topic_merge_enabled=False 时不执行
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.dao.message_repo import MessageRepo
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.group_merge import merge_topic_group
from app.services.llm import LLMError

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是群聊话题整理助手。下面是同一个群里消息分组的话题标签,每个标签附最近消息样本。\n"
    "请判断哪些标签讨论的是同一问题(同一故障案例/同一事项),应合并到其中一个已有标签。\n"
    "要求:\n"
    "1. 同一故障案例的开场症状报告与其后的诊断、处理、验证属于同一问题,必须合并"
    "(例如:亮度异常 与 本控按键故障处理)。\n"
    "2. 纯附件类标签(图片、视频、语音等)不指向具体问题,不得并入任何标签。\n"
    "3. 只合并能确定是同一台机器/同一次讨论的标签,拿不准时不要合并。\n"
    "4. 合并目标必须是输入中已存在的标签,不得新造。\n"
    "5. 只输出 JSON 对象 {\"待合并标签\": \"保留标签\"},无合并时输出 {}。不要输出任何解释。"
)


def _parse_mapping(raw: str, valid: set[str]) -> dict[str, str]:
    """解析 LLM 输出为 {旧标签: 新标签};非法项(未知标签/自映射)丢弃。"""
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        logger.warning("标签归并输出非 JSON: %r", text[:200])
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str):
            k, v = k.strip(), v.strip()
            if k in valid and v in valid and k != v:
                out[k] = v
    return out


def _resolve(mapping: dict[str, str]) -> dict[str, str]:
    """展开传递映射(A→B 且 B→C 则 A→C);成环的映射丢弃。"""
    out: dict[str, str] = {}
    for old, new in mapping.items():
        seen = {old}
        cur = new
        while cur in mapping and cur not in seen:
            seen.add(cur)
            cur = mapping[cur]
        if cur != old:
            out[old] = cur
    return out


def consolidate_labels(
    session: Session,
    llm,
    chat_id: str | None = None,
) -> list[tuple[str, str]]:
    """同群内用 LLM 判定同义标签并统一,返回实际应用的 [(旧标签, 新标签)]。

    应用映射后立即触发同标签归并(新标签下的多组合并为最早组)。
    LLM 调用失败只告警跳过该群,不影响其他群。
    """
    if not get_settings().topic_merge_enabled:
        return []
    repo = MessageRepo(session)
    chats: dict[str, set[str]] = {}
    for c, topic, _root, _fid in repo.list_all_topic_groups(chat_id=chat_id):
        chats.setdefault(c, set()).add(topic)

    applied: list[tuple[str, str]] = []
    for c, topics in sorted(chats.items()):
        if len(topics) < 2:
            continue
        samples = dict(repo.list_topic_openings(chat_id=c, limit=200))
        lines = "\n".join(
            f"- {t}: {samples.get(t) or '(无摘要)'}" for t in sorted(topics)
        )
        user_prompt = f"话题标签与最近消息样本:\n{lines}"
        try:
            raw = llm.chat(_SYSTEM_PROMPT, user_prompt)
        except LLMError as e:
            logger.warning("标签归并 LLM 调用失败 %s: %s", c, e)
            continue
        mapping = _resolve(_parse_mapping(raw, topics))
        if not mapping:
            continue
        # 标准标签=问题大类标准名,多案例共享:归并到标准名只改名不合并
        from app.services.topic_service import get_standard_labels

        standards = set(get_standard_labels(session))
        for old, new in mapping.items():
            session.execute(
                update(Message)
                .where(Message.chat_id == c, Message.topic == old)
                .values(topic=new)
            )
            session.execute(
                update(Conversation)
                .where(Conversation.chat_id == c, Conversation.topic == old)
                .values(topic=new)
            )
            applied.append((old, new))
            logger.info("标签归并 %s: %s -> %s", c, old, new)
        session.commit()
        for new in set(mapping.values()):
            if new in standards:
                continue
            merge_topic_group(session, c, new)
    return applied
