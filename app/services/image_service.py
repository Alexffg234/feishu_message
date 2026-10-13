"""飞书图片资源 -> 七牛对象存储服务。

背景:飞书图片消息的 content 里只有 `{"image_key": "img_v3_..."}`,image_key 是
临时凭证,会过期。它本身不是可直接展示的 URL。

这里在收到图片消息时(入库前)把 image_key 调飞书 `im/v1/images/:image_key`
下载成二进制,再上传到七牛对象存储,生成长期稳定的可访问 URL 返回给调用方。

设计原则(与项目其他服务一致):
- 未配置七牛 / 飞书无下载权限 / 网络异常 → 优雅降级返回空字符串,绝不抛出中断入库。
- 同一个 image_key 只处理一次(避免重复下载/上传)。
"""
from __future__ import annotations

import logging
from functools import lru_cache

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetImageRequest

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 飞书图片下载 token 类型为应用凭证(TENANT)
_IMG_PROVIDER = "qiniu"


class ImageService:
    """封装飞书图片下载 + 七牛上传,产出可展示 URL。"""

    def __init__(self, client: lark.Client | None = None):
        self._client = client

    # ------------------------------------------------------------ 对外
    def resolve_image_url(self, image_key: str) -> str:
        """把飞书 image_key 转成可展示的七牛 URL。

        返回七牛可访问 URL;下载/上传失败或未配置则返回空串(调用方保留 image_key)。
        """
        if not image_key:
            return ""

        settings = get_settings()
        if not settings.image_enabled:
            logger.debug("图片资源未启用(缺七牛/飞书配置),跳过 image_key=%s", image_key)
            return ""

        client = self._get_client()
        if client is None:
            logger.warning("飞书客户端不可用,跳过图片下载 image_key=%s", image_key)
            return ""

        # 下载飞书图片二进制
        data = self._download_image(client, image_key)
        if not data:
            logger.warning("下载飞书图片失败 image_key=%s", image_key)
            return ""

        # 上传到七牛,key 用 image_key(天然唯一,且能对回去原图)
        key = f"{_IMG_PROVIDER}/images/{image_key}"
        ok, _err = self._upload_to_qiniu(key, data)
        if not ok:
            logger.warning("上传图片到七牛失败 image_key=%s", image_key)
            return ""

        url = f"{settings.qiniu_domain.rstrip('/')}/{key}"
        logger.info("图片已落盘: %s -> %s", image_key, url)
        return url

    # ------------------------------------------------------------ 飞书下载
    def _download_image(self, client: lark.Client, image_key: str) -> bytes | None:
        """调飞书 im/v1/images/:image_key 下载图片二进制。失败返回 None。"""
        try:
            req = (
                GetImageRequest.builder()
                .image_key(image_key)
                .build()
            )
            resp = client.im.v1.image.get(req)
            if resp is None or not resp.success():
                logger.warning(
                    "获取飞书图片失败 image_key=%s: code=%s msg=%s",
                    image_key,
                    getattr(resp, "code", "?"),
                    getattr(resp, "msg", "?"),
                )
                return None
            file_io = getattr(resp, "file", None)
            if file_io is None:
                logger.warning("飞书图片响应无文件内容 image_key=%s", image_key)
                return None
            try:
                return file_io.read()
            except Exception as e:  # noqa: BLE001
                logger.warning("读取飞书图片字节失败 image_key=%s: %s", image_key, e)
                return None
        except Exception as e:  # noqa: BLE001 - 网络/权限等统一降级
            logger.warning("下载飞书图片异常 image_key=%s: %s", image_key, e)
            return None

    # ------------------------------------------------------------ 七牛上传
    def _upload_to_qiniu(self, key: str, data: bytes) -> tuple[bool, str]:
        """把字节上传到七牛。返回 (是否成功, 出错信息)。"""
        try:
            from qiniu import Auth, put_data

            settings = get_settings()
            auth = Auth(settings.qiniu_access_key, settings.qiniu_secret_key)
            token = auth.upload_token(settings.qiniu_bucket, key)
            ret, info = put_data(token, key, data=data)
            if info is not None and info.status_code == 200:
                return True, ""
            msg = _qiniu_info_message(info)
            return False, msg
        except Exception as e:  # noqa: BLE001
            logger.warning("七牛上传异常 key=%s: %s", key, e)
            return False, str(e)

    # ------------------------------------------------------------ 客户端
    def _get_client(self) -> lark.Client | None:
        if self._client is not None:
            return self._client
        settings = get_settings()
        if not (settings.app_id and settings.app_secret):
            return None
        return (
            lark.Client.builder()
            .app_id(settings.app_id)
            .app_secret(settings.app_secret)
            .build()
        )


def _qiniu_info_message(info) -> str:
    """从七牛上传返回的 info 提取可读错误信息。"""
    try:
        code = getattr(info, "status_code", "?")
        text_body = getattr(info, "text_body", None) or getattr(info, "text_body", "")
        return f"status={code} {str(text_body)[:200]}"
    except Exception:  # noqa: BLE001
        return "unknown qiniu error"


@lru_cache
def get_image_service() -> ImageService:
    """取图片服务单例。"""
    return ImageService()
