#!/usr/bin/env bash
# =====================================================================
# 飞书群聊采集服务 - 服务器热更新脚本
#
# 作用: 拉取 GitHub 最新代码 -> 更新依赖 -> 建表 -> 回填对话关系 -> 重启服务 -> 健康检查
# 用法: sudo bash deploy/update.sh
#
# 安全要点(兼容之前踩过的坑):
#   1. git fetch(纯网络) 与 git merge(纯本地) 分开执行,
#      避免 `git pull` 在合并阶段网络抖动导致整体失败(之前 "Empty reply from server" 的根源)。
#   2. 更新前先检查工作区是否干净: 有未提交改动会停下提示,绝不覆盖。
#   3. init_db.py / backfill_conversations.py 均幂等,可重复执行,不会重复建表/重复入库。
# =====================================================================

set -euo pipefail

# ---------------------------------------------------------------- 常量
# 所有路径/服务名都支持用环境变量覆盖(便于本地自测),缺省为服务器生产值。
PROJECT_DIR="${PROJECT_DIR:-/opt/feishu_group_message}"
SERVICE_NAME="${SERVICE_NAME:-feishu-message}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8008/health}"
# DRY_RUN=1 时不执行任何真实副作用(pip install / systemctl / init_db / backfill),
# 仅验证 git fetch + merge + 前置检查等纯逻辑,用于本地安全自测。
DRY_RUN="${DRY_RUN:-0}"

# 颜色输出
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[update]${NC} $*"; }
ok()    { echo -e "${GREEN}[ ok  ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn ]${NC} $*"; }
err()   { echo -e "${RED}[fail ]${NC} $*"; }

# 干跑桩: 模拟"真实副作用"命令,仅打印不执行
run_real() {
    if [[ "$DRY_RUN" == "1" ]]; then
        warn "[DRY_RUN] 跳过: $*"
        return 0
    fi
    "$@"
}

cd "$PROJECT_DIR"

# ---------------------------------------------------------------- 0. 前置检查
echo "============================================================"
info "飞书群聊采集服务 - 服务器热更新"
info "项目目录: ${PROJECT_DIR}"
info "git 远端: ${GIT_REMOTE}/${GIT_BRANCH}"
echo "------------------------------------------------------------"

if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    err "不是 git 仓库: $PROJECT_DIR"
    exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    err "找不到 venv python: $PYTHON_BIN (首次部署请先跑 deploy/install.sh)"
    exit 1
fi

# ---------------------------------------------------------------- 1. 工作区检查
info "[1/8] 检查工作区是否干净..."
if [[ -n "$(git status --porcelain)" ]]; then
    echo ""
    err "工作区有未提交的改动,为避免覆盖本地修改,已中止更新。"
    err "请先处理以下改动(备份或提交),再重跑本脚本:"
    echo ""
    git status --short
    echo ""
    exit 1
fi
ok "工作区干净,继续。"

# ---------------------------------------------------------------- 2. 拉取代码(fetch:纯网络,可重试)
info "[2/8] 拉取最新代码 (git fetch ${GIT_REMOTE} ${GIT_BRANCH})..."
FETCH_OK=0
for attempt in 1 2 3; do
    if git fetch "$GIT_REMOTE" "$GIT_BRANCH"; then
        FETCH_OK=1
        break
    fi
    warn "git fetch 第 ${attempt} 次失败(可能是网络闪断),${attempt}/3 次后重试..."
    sleep 3
done
if [[ "$FETCH_OK" != "1" ]]; then
    err "git fetch 连续失败,请检查网络后重试。"
    exit 1
fi
ok "代码已拉取到本地(objects 已下载)。"

# ---------------------------------------------------------------- 3. 合并且快进(纯本地,不联网)
info "[3/8] 合并到最新 (git merge --ff-only ...)..."
if ! git merge --ff-only "${GIT_REMOTE}/${GIT_BRANCH}"; then
    err "快进合并失败。可能原因: 本地与远端分叉,或远端有 force push。"
    err "请人工检查: git log --oneline -3 HEAD && git log --oneline -3 ${GIT_REMOTE}/${GIT_BRANCH}"
    exit 1
fi
CURRENT_COMMIT="$(git log --oneline -1)"
ok "已更新到: ${CURRENT_COMMIT}"

# ---------------------------------------------------------------- 4. 依赖安装(仅在 requirements.txt 变化时)
info "[4/8] 检查依赖是否需要更新..."
if [[ ! -f requirements.txt ]]; then
    warn "未找到 requirements.txt,跳过依赖安装(请确认代码完整)。"
elif command -v sha256sum >/dev/null 2>&1; then
    HASH_FILE="${PROJECT_DIR}/.deps.sha256"
    NEW_HASH="$(sha256sum requirements.txt 2>/dev/null | awk '{print $1}')" || true
    OLD_HASH="$([ -f "$HASH_FILE" ] && cat "$HASH_FILE" || true)"
    if [[ -n "$NEW_HASH" && "$NEW_HASH" != "$OLD_HASH" ]]; then
        warn "requirements.txt 有变化,执行 pip install..."
        run_real "${PYTHON_BIN}" -m pip install --upgrade -r requirements.txt
        echo "$NEW_HASH" > "$HASH_FILE"
        ok "依赖已更新。"
    else
        ok "依赖无变化,跳过安装。"
    fi
else
    warn "无 sha256sum,默认执行 pip install..."
    run_real "${PYTHON_BIN}" -m pip install --upgrade -r requirements.txt
fi

# ---------------------------------------------------------------- 5. 建表(幂等)
info "[5/8] 同步数据库表结构 (scripts/init_db.py)..."
run_real "${PYTHON_BIN}" scripts/init_db.py
ok "表结构就绪(幂等,重复执行安全)。"

# ---------------------------------------------------------------- 6. 回填对话关系(幂等)
info "[6/8] 回填存量对话关系 (scripts/backfill_conversations.py)..."
run_real "${PYTHON_BIN}" scripts/backfill_conversations.py
ok "对话关系回填完成(幂等)。"

# ---------------------------------------------------------------- 7. 重启服务 + 健康检查
info "[7/8] 重启服务 ${SERVICE_NAME}..."
run_real systemctl restart "${SERVICE_NAME}"
sleep 3

# ---------------------------------------------------------------- 8. 漏采周期回补定时器(幂等)
info "[8/8] 安装漏采回补定时器 feishu-backfill.timer..."
if [[ -f deploy/feishu-backfill.service && -f deploy/feishu-backfill.timer ]]; then
    TIMER_NEEDS=0
    [[ -f /etc/systemd/system/feishu-backfill.service ]] || TIMER_NEEDS=1
    cmp -s deploy/feishu-backfill.service /etc/systemd/system/feishu-backfill.service || TIMER_NEEDS=1
    cmp -s deploy/feishu-backfill.timer /etc/systemd/system/feishu-backfill.timer || TIMER_NEEDS=1
    if [[ "$TIMER_NEEDS" == "1" ]]; then
        run_real cp deploy/feishu-backfill.service deploy/feishu-backfill.timer /etc/systemd/system/
        run_real systemctl daemon-reload
    fi
    run_real systemctl enable --now feishu-backfill.timer
    ok "回补定时器就绪(每 5 分钟补最近 15 分钟,幂等)。"
else
    warn "未找到 feishu-backfill unit 文件,跳过定时器安装。"
fi

if [[ "$DRY_RUN" == "1" ]]; then
    warn "[DRY_RUN] 跳过 systemctl 状态检查"
elif ! systemctl is-active --quiet "${SERVICE_NAME}"; then
    err "服务未成功启动! 最近日志:"
    journalctl -u "${SERVICE_NAME}" -n 30 --no-pager
    exit 1
fi

# 健康检查(最多等 5 次 x 2s;DRY_RUN 时跳过)
if [[ "$DRY_RUN" == "1" ]]; then
    warn "[DRY_RUN] 跳过健康检查"
    HEALTH_OK=1
else
    HEALTH_OK=0
    for attempt in 1 2 3 4 5; do
        if [[ "$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL")" == "200" ]]; then
            HEALTH_OK=1
            break
        fi
        sleep 2
    done
fi

echo "------------------------------------------------------------"
if [[ "$HEALTH_OK" == "1" ]]; then
    ok "更新完成! 服务已重启并通过健康检查。"
else
    warn "服务已启动,但 /health 未在等待时间内返回 200。可查看日志:"
    warn "  journalctl -u ${SERVICE_NAME} -f"
fi
info "当前提交: ${CURRENT_COMMIT}"
info "验证总结页: 访问 http://<服务器>:8008/summary"
echo "============================================================"
