"""服务健康探活 + 异常告警。

拉取本机服务的 /healthz(可用性 + 数据新鲜度 + 队列积压 + 死信),
对异常项通过飞书机器人群机器人 webhook 推送告警。

检查项(任一命中即告警):
- 服务不可达 / /healthz 非 200 / db=false
- 最近入库时间超过 ALERT_NO_MESSAGE_MINUTES 分钟(数据不新鲜)
- 死信文件行数超过 ALERT_DEAD_LETTER_THRESHOLD(事件持续处理失败)

用法(项目根目录,服务器 venv):
    ./.venv/bin/python scripts/health_probe.py [--webhook URL] [--host 127.0.0.1] [--port 8008] [--dry-run]

告警 webhook 优先级: 命令行 --webhook > 环境变量 ALERT_WEBHOOK > config/.env 的 alert_webhook。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.config import get_settings  # noqa: E402


def _fetch_healthz(host: str, port: int, timeout: int = 10) -> tuple[int, dict | str]:
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _resolve_webhook(cli: str) -> str:
    return (cli or os.environ.get("ALERT_WEBHOOK") or get_settings().alert_webhook or "").strip()


def _send_webhook(webhook: str, title: str, lines: list[str], dry_run: bool) -> None:
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines) or "(无详情)"}
            ],
        },
    }
    if dry_run or not webhook:
        print("[dry-run] 将推送告警:")
        print(title)
        for ln in lines:
            print("  -", ln)
        return
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            print("告警已推送:", body)
    except Exception as exc:  # noqa: BLE001
        print("告警推送失败:", exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--webhook", default="", help="飞书机器人群机器人 webhook(覆盖配置)")
    parser.add_argument("--dry-run", action="store_true", help="只打印不真发")
    args = parser.parse_args()

    settings = get_settings()
    no_msg_min = int(getattr(settings, "alert_no_message_minutes", 0) or 0)
    dl_threshold = int(getattr(settings, "alert_dead_letter_threshold", 3))
    webhook = _resolve_webhook(args.webhook)

    status, data = _fetch_healthz(args.host, args.port)
    title = "✅ 服务正常" if (status == 200 and isinstance(data, dict) and data.get("db")) else "⚠️ 服务异常"
    problems: list[str] = []

    if not isinstance(data, dict):
        problems.append(f"服务不可达或 /healthz 异常: {data}")
    else:
        if not data.get("db"):
            problems.append("数据库连接失败 (db=false)")
        back = data.get("queue_backlog")
        if isinstance(back, int) and back < 0:
            problems.append("事件队列不可用 (queue_backlog=-1)")
        elif isinstance(back, int) and back > 900:
            problems.append(f"事件队列积压过高: {back}")
        dl_lines = (data.get("dead_letter") or {}).get("lines", 0)
        if dl_lines > dl_threshold:
            problems.append(f"死信事件数超阈值: {dl_lines} > {dl_threshold}")
        ago = data.get("last_message_seconds_ago")
        if no_msg_min > 0 and isinstance(ago, int) and ago >= 0 and ago > no_msg_min * 60:
            problems.append(f"最近 {ago // 60} 分钟没有新消息入库(阈值 {no_msg_min} 分钟)")
        if not problems and no_msg_min > 0 and ago == -1:
            problems.append("库里没有任何消息(last_message=-1)")

    if problems:
        title = "⚠️ 服务异常"

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {title} | healthz={data if isinstance(data, dict) else data}")
    if problems:
        print("发现问题:")
        for p in problems:
            print("  ×", p)
        _send_webhook(webhook, title, problems, args.dry_run)
        return 1

    print("无异常(未推送)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
