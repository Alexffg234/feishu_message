"""应用核心配置。

使用 pydantic-settings 统一管理配置,支持:
- 环境变量注入(Docker / 服务器)
- .env 文件(本地开发)
- 类型安全、自动解析
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """应用配置。所有字段均可通过同名环境变量覆盖(前缀 APP_ 可选)。"""

    model_config = SettingsConfigDict(
        env_file=(str(BASE_DIR / "config" / ".env"), str(BASE_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 飞书应用凭据 ----------
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""

    # ---------- 服务监听 ----------
    host: str = "0.0.0.0"
    port: int = 8008
    # 生产环境 worker 数(uvicorn/gunicorn)
    workers: int = 1

    # ---------- 采集过滤 ----------
    # 允许处理的群 chat_id,逗号分隔,空 = 全部
    allowed_chat_ids: str = ""

    # ---------- 数据库 ----------
    # SQLAlchemy 连接串,如:
    #   sqlite:///./data/messages.db          (本地开发,零配置)
    #   postgresql+psycopg://user:pwd@host:5432/dbname  (生产)
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'messages.db').as_posix()}"

    # ---------- LLM 主题分类(OpenAI 兼容接口) ----------
    # 任意提供 /chat/completions 的服务均可(OpenAI / Haier model / vLLM ...)
    # api_key 为空时自动禁用主题分类,消息仍正常入库(只是不归类)。
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = 30

    # ---------- 图片资源(飞书 image_key -> 七牛对象存储) ----------
    # 收到图片消息时,调飞书下载二进制并上传到七牛,生成可展示 URL 存入消息。
    # 未配置七牛或飞书无下载权限时,保留 image_key,优雅降级不报错。
    qiniu_access_key: str = ""
    qiniu_secret_key: str = ""
    qiniu_bucket: str = ""
    qiniu_domain: str = ""  # 七牛 CDN 访问域名,如 https://cdn.example.com(不带尾斜杠)
    # 是否启用图片下载落盘;默认开启,未配置七牛仍会自动跳过
    image_download_enabled: bool = True

    # ---------- 轮次合并(同一人连发多条) ----------
    # 同一发送者的独立消息,间隔小于该秒数且中间无无关打断时,合并进同一消息块。
    # 0 = 禁用合并(每条独立消息自成一组)。
    # 默认 300:覆盖「先甩截图、随后打字提问」的节奏(中间有他人插话则不并)。
    turn_merge_seconds: int = 300

    # ---------- 同主题归组 ----------
    # LLM 主题分类完成后,同群内相同 topic 标签的回复链组自动归并为一个对话
    # (「直接发言无回复」的碎片反馈靠标签归回原问题块,不依赖时间窗)。
    # False = 只打标签不归并。
    topic_merge_enabled: bool = True

    # ---------- 会话缝合(后台周期 LLM 判定同一案例续聊) ----------
    # 标准标签只归类不合并,同一案例的后续会话由本调度自动缝合合并
    # (LLM 判定「同一问题处理生命周期」,护栏:标签不冲突)。
    # 缝合并入的续聊视为同一问题不断句(不画横杠)。
    # False 或 0 = 禁用后台调度(仅手动脚本 scripts/stitch_conversations.py)。
    conv_stitch_enabled: bool = True
    conv_stitch_interval_minutes: int = 30

    # ---------- 主题 AI 总结(后台定时全量预生成) ----------
    # 每 N 分钟扫一遍全部 topic,指纹(消息数/最大消息 id)变化的重新生成 AI 分析。
    # 0 = 禁用后台定时(仅手动 POST 触发)。
    topic_summary_interval_minutes: int = 10

    # ---------- 访问控制(登录注册)----------
    # true = 除白名单(登录页/认证接口/健康检查/飞书事件回调)外,全部要求登录
    # false = 关闭认证(仅本地调试用,切勿在生产开启)
    auth_enabled: bool = True
    # 是否开放自助注册;false 时只能用 scripts/create_user.py 建号
    # (库里尚无账号时,注册始终开放,保证首次部署能创建管理员)
    allow_public_register: bool = True
    # 登录态有效期(小时)
    session_ttl_hours: int = 12
    # 会话 cookie 签名密钥;留空则自动生成并持久化到 data/.session_secret
    # 生产建议显式配置(多机部署时各实例需一致,否则会互相踢下线)
    session_secret: str = ""
    # 会话 cookie 是否仅 HTTPS 传输;站点为 https 时必须置 true
    cookie_secure: bool = False

    # ---------- 日志 ----------
    log_level: str = "INFO"
    log_file: str = ""  # 空=仅控制台; 否则同时写入文件(滚动)

    # ---------- 内部 / 派生 ----------
    @property
    def event_path(self) -> str:
        return "/feishu/event"

    @property
    def allowed_chat_id_set(self) -> set[str]:
        return {c.strip() for c in self.allowed_chat_ids.split(",") if c.strip()}

    @property
    def is_ready(self) -> bool:
        """核心凭据是否就绪(用于启动自检与健康检查)。"""
        return bool(self.app_id and self.app_secret and self.verification_token)

    @property
    def llm_enabled(self) -> bool:
        """LLM 主题分类是否可用(api_key 与 model 均配置)。"""
        return bool(self.llm_api_key and self.llm_model)

    @property
    def image_enabled(self) -> bool:
        """图片资源落盘是否可用(开关开启且七牛配置齐全+飞书凭据就绪)。"""
        return bool(
            self.image_download_enabled
            and self.qiniu_access_key
            and self.qiniu_secret_key
            and self.qiniu_bucket
            and self.qiniu_domain
            and self.app_id
            and self.app_secret
        )


@lru_cache
def get_settings() -> Settings:
    """获取配置单例(带缓存,避免重复读 .env)。"""
    return Settings()
