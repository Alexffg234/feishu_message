"""处理阶段(stage)判定:与 topic 正交的第二个维度。

为什么需要它
------------
topic 回答「这是什么问题」(现象),stage 回答「处理到哪一步了」(进展)。
历史数据把两者混在同一棵树里:「已推」「上门维修」「远程删除软件」
「取消推送更新」都被 LLM 当成了主题标签。后果有两层:
1. 标签体系被动作词污染,大类的统计口径不再纯净;
2. 更严重的是「已推」不是标准标签,会触发同标签物理合并——
   4 天里 4 件不相干的事被并进同一个对话,事后不可拆分。

所以:现象归 topic(闭集),进展归 stage(规则),两者互不干扰。

判定策略
--------
纯规则、零 LLM 成本,按「句子」而不是整段匹配:
1. 先按标点切句,丢弃疑问句(「能推送解决吗」不能算「已推送」);
2. 再按优先级 已解决 > 已上门 > 已推送 > 处理中 取最高一档;
3. 都没有实质处理动作时,有实质内容就是「待处理」,否则留空(未判定)。

规则粗,但确定性好、可随时调、不花钱。要更准就直接改这里的正则,
不需要重跑 LLM。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

logger = logging.getLogger(__name__)

# 阶段取值(前端/统计按此枚举,勿随意新增同义值)
STAGE_PENDING = "待处理"
STAGE_DOING = "处理中"
STAGE_PUSHED = "已推送"
STAGE_ONSITE = "已上门"
STAGE_RESOLVED = "已解决"

STAGES = (
    STAGE_PENDING,
    STAGE_DOING,
    STAGE_PUSHED,
    STAGE_ONSITE,
    STAGE_RESOLVED,
)

# 分句:中英文标点 + 换行。逐句判断可避免「前半句已推、后半句提问」被整体误判
_CLAUSE_SPLIT = re.compile(r"[。．.！!？?；;\n\r，,、]+")

# 疑问句特征:问号结尾或含征询语气
_QUESTION_TAIL = ("吗", "呢", "嘛")
_QUESTION_RE = re.compile(
    r"能不能|能否|可不可以|是否可以|有没有|是不是|怎么办|怎么处理|怎么弄|如何"
)

# 已解决:明确的问题消失/修复表述
_RESOLVED_RE = re.compile(
    r"已解决|解决了|已修复|修复了|已恢复|恢复正常|正常了|搞定了|已搞定|"
    r"问题消除|已处理完|已处理好|好了|可以了|成功了|没问题了"
)

# 已上门:线下渠道已介入
_ONSITE_RE = re.compile(r"已上门|上门了|上门处理|师傅到|已到现场|到现场了|现场处理")

# 已推送:版本/软件已下发(只认完成态,「推送一下」「能不能推」靠疑问句过滤兜底)
_PUSHED_RE = re.compile(
    r"已推|推送了|推了一版|推了个|推个新版|推了一版|推过去了|已升级|"
    r"升级了|给您推|推了版|推版|已下发|下发了"
)

# 处理中:已开始排查但尚未有结论(注意不含「看下/看看」——
# 那多是「麻烦看下」这类请求,意味着还没开始,应落在待处理)
_DOING_RE = re.compile(
    r"排查|远程|日志|log|稍后|处理中|分析|跟进|维护|复现|正在|稍等|查一下|"
    r"处理一下|晚点|编个版本"
)

# 「恢复出厂」是重置动作,不代表问题已解决,不能触发已解决判定
_RESOLVED_EXCLUDE = re.compile(r"恢复出厂|恢复默认")

# 纯回应句(整句只有客套/确认),不作为实质内容
_FILLER_ONLY_RE = re.compile(
    r"^(?:好的?|收到|谢谢|感谢|嗯+|哦+|行|可以|明白|了解|知道了|辛苦了|麻烦了|ok)+$",
    re.IGNORECASE,
)

# 「待处理」的最低信息量门槛(字),过滤「好的」「收到」这类无问题内容的短句
_MIN_SUBSTANTIVE = 4

# 飞书 @ 占位符(如 @_user_1):不含信息量,长度判断前先剔除,
# 否则「@_user_1 111」会因占位符而被当成实质内容
_MENTION_RE = re.compile(r"@_user_\d+")


def _is_question(clause: str) -> bool:
    """判断一句是否是提问(提问里的动作词不能当完成态)。"""
    c = clause.strip()
    if not c:
        return False
    if "？" in c or "?" in c:
        return True
    if c.endswith(_QUESTION_TAIL):
        return True
    return bool(_QUESTION_RE.search(c))


def _clauses(texts: Iterable[str]) -> list[str]:
    """把多条消息切成「非提问、非纯客套」句子,保留原顺序。"""
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        for clause in _CLAUSE_SPLIT.split(text):
            clause = _MENTION_RE.sub("", clause).strip()
            if not clause or _is_question(clause):
                continue
            if _FILLER_ONLY_RE.match(clause):
                continue
            out.append(clause)
    return out


def detect_stage(texts: Iterable[str]) -> str:
    """从一组消息文本判定处理阶段。返回 STAGES 之一或空串(未判定)。"""
    clauses = _clauses(texts)
    if not clauses:
        return ""

    for clause in clauses:
        if _RESOLVED_RE.search(clause) and not _RESOLVED_EXCLUDE.search(clause):
            return STAGE_RESOLVED
    if any(_ONSITE_RE.search(c) for c in clauses):
        return STAGE_ONSITE
    if any(_PUSHED_RE.search(c) for c in clauses):
        return STAGE_PUSHED
    if any(_DOING_RE.search(c) for c in clauses):
        return STAGE_DOING
    # 有实质内容但没有任何处理动作 → 挂在待处理池里
    if any(len(c) >= _MIN_SUBSTANTIVE for c in clauses):
        return STAGE_PENDING
    return ""


def update_group_stage(session, chat_id: str, root_message_id: str) -> str:
    """重算并写回某回复链组的 stage(messages 与 conversations 两侧)。

    不调 LLM,每次消息入库后调用,保证「已推/已上门/已解决」随对话推进实时更新,
    不依赖该组是否已经打过标签。失败只告警,不影响入库。
    """
    from app.dao.conversation_repo import ConversationRepo
    from app.dao.message_repo import MessageRepo

    if not chat_id or not root_message_id:
        return ""
    repo = MessageRepo(session)
    rows = repo.list_by_root(root_message_id, chat_id)
    if not rows:
        return ""
    stage = detect_stage(m.text for m in rows)
    repo.set_stage_for_group(chat_id, root_message_id, stage)
    ConversationRepo(session).set_stage(chat_id, root_message_id, stage)
    session.commit()
    return stage
