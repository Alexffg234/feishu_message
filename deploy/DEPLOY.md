# 部署教程：把飞书群聊采集服务部署到你的 Linux 服务器

> 本教程面向「Linux 云服务器 + 已有域名」场景，按步骤照抄即可。每步都标注了「期望看到的结果」用于自检。

---

## 一、准备阶段（提前确认）

| 事项 | 要求 | 你确认 |
|------|------|--------|
| Linux 服务器 | Ubuntu 20/22 或 CentOS 7/8，有 root | ☐ |
| 域名 | 已解析 A 记录到服务器公网 IP | ☐ |
| 端口 | 安全组/防火墙放行 80、443 | ☐ |
| 数据库 | 远程 MySQL `feishu_message` 库已建好，服务器能连 | ☐ |

> **先测域名解析**（在本地电脑执行）：
> ```bash
> ping your-domain.com
> # 应返回你服务器的公网 IP
> ```

---

## 二、本地：把代码推到 GitHub

在**你本地电脑**的终端执行（项目在 `D:\Code\Vib Coding\feishu_group_message`）：

```bash
cd "D:/Code/Vib Coding/feishu_group_message"
git push -u origin main
```

> 这一步会弹 GitHub 登录。代码已脱敏，`config/.env`（含所有机密）不会被推送。

**期望看到**：`main -> main` 推送成功，GitHub 上有 `app/`、`deploy/`、`config/.env.example` 等文件，**没有** `config/.env`。

---

## 三、登录服务器

```bash
ssh root@你的服务器IP
# 或 ssh ubuntu@你的服务器IP
```

---

## 四、克隆项目 + 一键部署

```bash
# 1. 克隆你的仓库
git clone https://github.com/Alexffg234/feishu_message.git /opt/feishu_group_message
cd /opt/feishu_group_message

# 2. 运行一键部署脚本
sudo bash deploy/install.sh /opt/feishu_group_message
```

**脚本会依次做**：装系统依赖 → 建 Python venv → 装依赖包 → 放行防火墙 → 检查 `.env`。

> **首次运行时**，如果没有 `config/.env`，脚本会从模板生成并**提示你停下填配置**。此时：

```bash
# 3. 编辑配置（把真实值填进去）
vim /opt/feishu_group_message/config/.env
```

`.env` 需要填的关键项：
```dotenv
APP_ID=cli_你的AppID
APP_SECRET=你的AppSecret
VERIFICATION_TOKEN=你的VerificationToken
ENCRYPT_KEY=            # 留空(明文模式)
DATABASE_URL=mysql+pymysql://root:你的数据库密码@你的数据库IP:3306/feishu_message?charset=utf8mb4
```

> **填完后重新执行一遍部署脚本**，完成建表 + 启动服务：
> ```bash
> sudo bash deploy/install.sh /opt/feishu_group_message
> ```

**期望看到**：
```
消息入库 ...
部署完成!
/health OK
/ready OK
```

> ⚠️ 如果你不想用一键脚本，可用「手动步骤」替代，见文末附录 A。

---

## 五、确认服务运行

```bash
systemctl status feishu-message
curl http://127.0.0.1:8008/health    # 期望 {"status":"ok"}
curl http://127.0.0.1:8008/ready     # 期望数据库连通 OK
```

查看实时日志：
```bash
journalctl -u feishu-message -f
```

可选：启用「漏采周期回补」定时器（每 5 分钟自动补最近 15 分钟的漏采消息，幂等；
`deploy/update.sh` 热更新时会自动安装，手工部署时需手动执行一次）：
```bash
cp /opt/feishu_group_message/deploy/feishu-backfill.service /opt/feishu_group_message/deploy/feishu-backfill.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now feishu-backfill.timer
systemctl list-timers feishu-backfill.timer   # 应看到 NEXT 时间
```

---

## 六、配置 Nginx（HTTP）

```bash
# 1. 建立可用配置
cp /opt/feishu_group_message/deploy/nginx-feishu.conf /etc/nginx/sites-available/feishu-message

# 2. 编辑，把 your-domain.com 全部替换成你的域名
vim /etc/nginx/sites-available/feishu-message
#   vim 里全局替换: 输入 :%s/your-domain.com/你的域名/g  然后 :wq

# 3. 启用站点
ln -s /etc/nginx/sites-available/feishu-message /etc/nginx/sites-enabled/

# 4. 测试并重载
nginx -t          # 期望: syntax is ok, test is successful
nginx -s reload
```

**期望看到**：
```bash
curl http://你的域名/health    # 期望 {"status":"ok"} （走 Nginx 反代，不需要 https）
```

---

## 七、申请 HTTPS 证书

```bash
# 安装 certbot
apt-get install -y certbot python3-certbot-nginx   # Ubuntu/Debian
# 或: yum install -y certbot python3-certbot-nginx   # CentOS

# 申请证书（自动改 Nginx 配置）
certbot --nginx -d 你的域名
```

按提示输入邮箱、同意条款。**期望看到**：`Congratulations! Your certificate and chain have been saved...`

**验证 HTTPS**：
```bash
curl https://你的域名/health    # 期望 {"status":"ok"}
```

---

## 八、更新飞书后台回调地址

1. 打开 [飞书开放平台](https://open.feishu.cn) → 你的应用 → **事件与回调**。
2. 把**请求地址**从 ngrok 地址改成本服务器域名：
   ```
   https://你的域名/feishu/event
   ```
3. 点保存 → 飞书会发 URL 校验请求，服务端应自动响应 **challenge**。
   - 若成功，飞书会提示"保存成功"。
4. **关键**：修改后到「版本管理与发布」**创建一个新版本并申请发布**，审核通过后才真正生效（和之前踩的坑一样）。

> ⚠️ 改回调地址后，ngrok 隧道就可以不用了（关掉它即可）。

---

## 九、最终验证

```bash
# 1. 服务健康
curl https://你的域名/health

# 2. 查已采集消息
cd /opt/feishu_group_message
.venv/bin/python scripts/query_messages.py
```

**到群里再发一条消息**（如"你好"）。期望：
- 服务日志出现 `im.message.receive_v1` 事件 + "消息入库"
- `query_messages.py` 出现新记录

---

## 附录 A：手动部署步骤（不使用一键脚本）

```bash
# 系统依赖
apt-get install -y python3 python3-venv python3-pip nginx git

# 项目
cd /opt
git clone https://github.com/Alexffg234/feishu_message.git feishu_group_message
cd feishu_group_message

# venv + 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt gunicorn

# 配置
cp config/.env.example config/.env
vim config/.env     # 填真实值

# 建表
.venv/bin/python scripts/init_db.py

# systemd 服务
cp deploy/feishu-message.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now feishu-message
systemctl status feishu-message
```

---

## 附录 B：常见问题排错

| 症状 | 排查 |
|------|------|
| 回调校验失败 | Nginx 生效？HTTPS 证书正常？`/feishu/event` 路径对？服务在 8008 `curl 127.0.0.1:8008/health` OK？ |
| 服务起不来 | `journalctl -u feishu-message -e` 看报错，多因 `.env` 或数据库 |
| 收不到消息 | 应用是否**重新发布**？机器人是否在群里？权限审批是否通过？ |
| 权限报错 | 服务默认以 root 运行。若改用低权限用户，需确保项目目录属主一致：`chown -R <user>:<group> /opt/feishu_group_message` |

---

## 安全提醒

- `config/.env` 含机密，**绝不提交到 Git**（已在 `.gitignore`）。
- 数据库生产环境建议用独立账号，不要和开发共用密码。
- 服务只监听 `127.0.0.1:8008`，公网仅暴露 80/443 经 Nginx，安全。
