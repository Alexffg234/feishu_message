"""设备标识抽取器(话题归纳阶段二)。

从消息文本中抽取出"设备标识",作为「同一故障案例」的确定性锚点:
- 归并/缝合时 case_key 相等(且非空)即视为同一案例,不依赖 LLM;
- 纯规则 + 每消息一次正则,成本极低。

抽取优先级(命中即返回,类型前缀防串案):
  1. UUID            —— 唯一性最强
  2. 带标签的 SN      —— 需出现 "SN/序列号/机身号/设备编号" 等词才采信(防把订单号当设备号)
  3. MAC 地址         —— 排除全 0 / 全 F / 广播
  4. 带标签的公网 IP  —— 排除私有/回环/保留段

全部未命中返回 ""(未识别)。
"""
from __future__ import annotations

import re

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}[-][0-9a-fA-F]{4}[-][0-9a-fA-F]{4}"
    r"[-][0-9a-fA-F]{4}[-][0-9a-fA-F]{12}\b"
)
# 带标签的设备编号:需要 "SN/序列号/机身号/设备编号" 等引导词
_SN_RE = re.compile(
    r"(?i)\b(?:sn|序列号|机身号|设备编号|出厂编号)\s*[:：=]?\s*([A-Za-z0-9\-_]{6,32})\b"
)
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
# 公网 IP:排除 0/10/127/172.16-31/192.168/255 与直接跟端口
_IP_RE = re.compile(
    r"(?i)\b(?:ip|ip地址|地址)\s*[:：=]?\s*(\d{1,3}(?:\.\d{1,3}){3})\b"
)
_PRIVATE_IP = re.compile(
    r"^(?:0\.|10\.|127\.|255\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
)
_FULL_MAC = re.compile(r"^(?:00[:.-]?){6}$|^(?:ff[:.-]?){6}$", re.IGNORECASE)


def _clean_sn(sn: str) -> str:
    """SN 后处理:只保留字母数字与连字符,去掉误吞的后缀。"""
    keep = re.sub(r"[^A-Za-z0-9\-]", "", sn)
    return keep.strip("-_")


def extract_case_key(text: str) -> str:
    """从一句消息文本抽取设备标识;无命中返回 ""。"""
    if not text:
        return ""

    m = _UUID_RE.search(text)
    if m:
        return "uuid:" + m.group(0).upper()

    m = _SN_RE.search(text)
    if m:
        sn = _clean_sn(m.group(1))
        if len(sn) >= 6:
            return "sn:" + sn.upper()

    m = _MAC_RE.search(text)
    if m:
        mac = m.group(0).upper()
        if not _FULL_MAC.match(mac.replace(":", "-").replace(".", "-")):
            return "mac:" + mac.replace(":", "-")

    m = _IP_RE.search(text)
    if m:
        ip = m.group(1)
        if not _PRIVATE_IP.match(ip):
            parts = ip.split(".")
            if all(0 <= int(p) <= 255 for p in parts):
                return "ip:" + ip

    return ""


def conversation_case_key(messages) -> str:
    """取一组合并候选消息(通常同对话)的代表设备标识:按时间升序取首个非空。

    供归并/缝合判定两个会话是否同一案例;都为空则无法确定性判定。
    """
    for m in messages or []:
        key = (getattr(m, "case_key", None) or "").strip()
        if key:
            return key
    return ""
