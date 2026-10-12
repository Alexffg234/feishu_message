#!/usr/bin/env bash
# ============================================================
# 飞书群聊消息采集 - Linux 服务器一键部署脚本
# 支持: Ubuntu / Debian / CentOS / Rocky (systemd)
#
# 用法: sudo bash deploy/install.sh /opt/feishu_group_message
#   （可省略路径,默认 /opt/feishu_group_message）
# ============================================================
set -euo pipefail

DEST="${1:-/opt/feishu_group_message}"
SERVICE_NAME="feishu-message"

echo "=============================================="
echo "  飞书群聊消息采集 部署脚本"
echo "  目标目录: $DEST"
echo "=============================================="

# ---------- 0. 检查 root ----------
if [ "$(id -u)" -ne 0 ]; then
  echo "错误: 请用 root 执行 (sudo bash $0)"
  exit 1
fi

# ---------- 1. 系统依赖 ----------
echo "[1/8] 安装系统依赖..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip nginx git curl
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip nginx git curl
else
  echo "错误: 未识别的包管理器(仅支持 apt/yum)"
  exit 1
fi

# ---------- 2. 拷贝项目 ----------
echo "[2/8] 准备项目目录 $DEST ..."
if [ ! -d "$DEST" ]; then
  # 方式A: 从 GitHub 拉取(推荐,确保脱敏)
  read -rp "请输入你的 GitHub 仓库地址(留空则手动拷贝): " REPO_URL
  if [ -n "$REPO_URL" ]; then
    git clone "$REPO_URL" "$DEST"
  else
    echo "请在本地把项目文件拷贝到 $DEST 后再继续"
    mkdir -p "$DEST"
    echo "请上传项目文件到 $DEST,然后重新运行本脚本"
    exit 1
  fi
fi
cd "$DEST"

# ---------- 3. Python 虚拟环境 ----------
echo "[3/8] 创建 Python 虚拟环境..."
python3 -m venv .venv
source .venv/bin/activate
echo "[3/8] 安装 Python 依赖..."
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn

# ---------- 4. 防火墙放行 ----------
echo "[4/8] 防火墙放行 80/443..."
if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp || true
  ufw allow 443/tcp || true
fi

# ---------- 5. 配置 .env ----------
echo "[5/8] 检查配置文件 config/.env ..."
if [ ! -f "config/.env" ]; then
  cp config/.env.example config/.env
  echo "已生成 config/.env,请编辑填入真实凭据后再继续!"
  echo "  必填: APP_ID / APP_SECRET / VERIFICATION_TOKEN / DATABASE_URL"
  echo "  提示: 服务默认监听 0.0.0.0:8008,由 Nginx 反代"
  exit 1
else
  echo "config/.env 已存在,跳过"
fi

# 确保项目目录属于 root（脚本以 root 运行）
chown -R root:root "$DEST" 2>/dev/null || true

# ---------- 6. 初始化数据库 ----------
echo "[6/8] 初始化数据库表..."
( cd "$DEST" && .venv/bin/python scripts/init_db.py )

# ---------- 7. 安装 systemd 服务 ----------
echo "[7/8] 安装 systemd 服务..."
# 修正服务文件中的工作目录和路径
SERVICE_FILE="$DEST/deploy/$SERVICE_NAME.service"
if [ -f "$SERVICE_FILE" ]; then
  sed -i "s|/opt/feishu_group_message|$DEST|g" "$SERVICE_FILE"
  cp "$SERVICE_FILE" /etc/systemd/system/$SERVICE_NAME.service
  systemctl daemon-reload
  systemctl enable $SERVICE_NAME
  systemctl restart $SERVICE_NAME
  echo "  服务已启动: systemctl status $SERVICE_NAME"
else
  echo "警告: 未找到 $SERVICE_FILE,请手动创建 systemd 服务"
fi

# ---------- 8. 健康检查 ----------
echo "[8/8] 等待服务就绪并健康检查..."
sleep 3
curl -s http://127.0.0.1:8008/health && echo "  <- /health OK"
curl -s http://127.0.0.1:8008/ready  && echo "  <- /ready OK"

echo ""
echo "=============================================="
echo "  部署完成!"
echo ""
echo "  接下来你需要:"
echo "  1) 配置 Nginx: 复制 deploy/nginx-feishu.conf 到 /etc/nginx/sites-available/"
echo "     修改其中的 your-domain.com 为你的域名"
echo "  2) 申请 HTTPS 证书:  sudo certbot --nginx -d your-domain.com"
echo "  3) 把飞书后台回调地址改成: https://your-domain.com/feishu/event"
echo "  4) 重启 Nginx:  sudo systemctl reload nginx"
echo "=============================================="
