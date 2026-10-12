# 部署指南（Linux 云服务器 + 域名）

本文档指导把飞书群聊采集服务部署到 **Linux 云服务器**，通过 **Nginx + HTTPS** 对外提供回调。

## 架构

```
[飞书服务器] --HTTPS 事件推送--> 你的域名 (443)
                                    ↓
                              Nginx (反向代理 + HTTPS)
                                    ↓
                        127.0.0.1:8008 (Gunicorn + FastAPI)
                                    ↓
                     远程 MySQL (如 YOUR_DB_HOST:3306)
```

- **Nginx**：终结 HTTPS，把 `/feishu/event` 等路径代理到本机服务。
- **Gunicorn + UvicornWorker**：生产级 WSGI/ASGI 运行，多 worker。
- **MySQL**：数据持久化（远程或服务器本机均可）。

## 前置条件

1. Linux 服务器（Ubuntu/Debian/CentOS），有 root 权限，可 SSH。
2. 已注册域名并做 **A 记录解析**到服务器公网 IP。
3. 服务器安全组/防火墙放行 **80、443** 端口。
4. 远程 MySQL 已建好库（如 `feishu_message`），且服务器能连上。

## 一、项目上线准备（本地）

### 1. 提交并推送代码（已脱敏）
项目已配置 `.gitignore`，`config/.env` 不会入库。确保推到 GitHub：
```bash
git add -A
git commit -m "add deploy files"
git push -u origin main
```

### 2. 准备服务器侧 `.env`
生产环境的 `config/.env` 在服务器上创建，**不入库**。

## 二、在服务器上部署

### 方式 A：一键脚本（推荐）
把项目推到 GitHub 后，在服务器上执行：
```bash
# 上传脚本后执行（也可在本地先编辑）
sudo bash deploy/install.sh /opt/feishu_group_message
```
脚本会自动：装系统依赖 → 建 venv → 装 Python 依赖 → 放行防火墙 → 生成/检查 `.env` → 初始化数据库 → 启动 systemd 服务 → 健康检查。

> 如果走「手动拷贝」路径（没有 GitHub 仓库），请把 `deploy/`、`app/`、`config`、`requirements.txt`、`scripts/` 一起拷到目标目录。

建完 `.env` 后编辑它，填入真实凭据：
```dotenv
APP_ID=cli_xxxxxxxxxxxx
APP_SECRET=xxxx
VERIFICATION_TOKEN=xxxx
ENCRYPT_KEY=
DATABASE_URL=mysql+pymysql://root:YOUR_DB_PASSWORD@YOUR_DB_HOST:3306/feishu_message?charset=utf8mb4
```
然后重新运行一次脚本完成后续步骤。

### 方式 B：手动步骤
```bash
# 1. 系统依赖
apt-get install -y python3 python3-venv python3-pip nginx git  # 或 yum install

# 2. 项目
cd /opt
git clone <你的仓库> feishu_group_message
cd feishu_group_message

# 3. venv + 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt gunicorn

# 4. 配置
cp config/.env.example config/.env
vim config/.env   # 填真实凭据 + DATABASE_URL

# 5. 建表
.venv/bin/python scripts/init_db.py

# 6. systemd 服务
cp deploy/feishu-message.service /etc/systemd/system/
sed -i 's#/opt/feishu_group_message#/opt/feishu_group_message#g' /etc/systemd/system/feishu-message.service
systemctl daemon-reload
systemctl enable --now feishu-message
systemctl status feishu-message
```

## 三、配置 Nginx + HTTPS

```bash
# 1. 把配置拷到 nginx
cp deploy/nginx-feishu.conf /etc/nginx/sites-available/feishu-message
vim /etc/nginx/sites-available/feishu-message   # 把 your-domain.com 改成你的域名
ln -s /etc/nginx/sites-available/feishu-message /etc/nginx/sites-enabled/
nginx -t && nginx -s reload

# 2. 申请 HTTPS 证书（certbot）
apt-get install -y certbot python3-certbot-nginx   # 或 yum install certbot
certbot --nginx -d your-domain.com
```

## 四、更新飞书后台回调地址

1. 打开飞书开放平台 → 你的应用 → 「事件与回调」。
2. 把 **请求地址** 从 ngrok 地址改成本服务器域名：
   ```
   https://your-domain.com/feishu/event
   ```
3. 保存。飞书会发起一次 URL 校验（challenge），服务端会自动响应。
4. 修改回调地址后需**重新发布应用版本**（新版本含新回调地址），审核通过后生效。

## 五、验证

```bash
# 服务健康
curl https://your-domain.com/health     # {"status":"ok"}
curl https://your-domain.com/ready      # 数据库连通检查

# 查消息
cd /opt/feishu_group_message
.venv/bin/python scripts/query_messages.py
```

到群里再发一条消息，`query_messages.py` 应出现新记录；`journalctl -u feishu-message -f` 可看实时日志。

## 常见问题

- **回调校验失败**：确认 Nginx 已生效、HTTPS 证书正常、`/feishu/event` 路径正确、服务在 8008 运行。
- **服务起不来**：`journalctl -u feishu-message -e` 看错误；多半是 `.env` 没填对或数据库连不上。
- **权限问题**：设置 `User=www-data` 时确保项目目录属主是 `www-data`。
- **多 worker 与顺序**：默认 2 worker。若业务要求严格保序，可改 `--workers 1`。

## 安全提醒

- `config/.env` 含 APP_SECRET / 数据库密码，**务必不要提交到 Git**（已在 `.gitignore`）。
- 数据库密码建议不要和开发环境共用，生产使用独立账号更安全。
- Nginx 只暴露 80/443；服务本身监听 127.0.0.1:8008，不直接暴露公网。
