"""飞书消息资源(file_key) -> 七牛对象存储服务。

背景:飞书图片/视频消息的内容里只有资源 key(image_key / file_key),这些 key 是
临时凭证,会过期,本身不是可直接展示的 URL。

这里在收到资源类消息时(入库前)用飞书「获取消息中的资源文件」接口把
file_key 下载成二进制,再上传到七牛对象存储,生成长期稳定的可访问 URL。

接口说明(飞书 im/v1/messages/:message_id/resources/:file_key,type 必填):
- type=image   图片、富文本里的图片 → file_key 用 image_key
- type=file    文件、音频、视频(表情除外) → file_key 用 file_key
注意:不能用 /im/v1/images/:image_key(那是给机器人自己上传的图用,下用户资源报 234001)。

设计原则(与项目其他服务一致):
- 未配置七牛 / 飞书无下载权限 / 网络异常 → 优雅降级返回空字符串,绝不抛出中断入库。
"""
from __future__ import annotations

import logging
import mimetypes
import os
import tempfile
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetMessageResourceRequest

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 飞书资源下载 token 类型为应用凭证(TENANT)
_PROVIDER = "qiniu"
# 飞书接口允许的资源类型
ResType = Literal["image", "file"]


class ImageService:
    """封装飞书消息资源(file_key)下载 + 七牛上传,产出可展示 URL。"""

    def __init__(self, client: lark.Client | None = None):
        self._client = client

    # ------------------------------------------------------------ 对外
    def resolve_image_url(self, image_key: str, message_id: str = "") -> str:
        """把飞书图片资源(image_key)转成可展示的七牛 URL。"""
        return self._resolve_resource(image_key, message_id, "image", "images")

    def resolve_file_url(self, file_key: str, message_id: str = "", file_name: str = "") -> str:
        """把飞书视频/文件资源(file_key)转成可展示的七牛 URL(type=file)。

        file_name: 原始文件名(如 "新建文本文档.txt"),用于推断上传 MIME 类型
        和给下载 URL 追加 ?attname,保证浏览器按正确文件名+扩展名下载。
        """
        return self._resolve_resource(file_key, message_id, "file", "files", file_name=file_name)

    def _resolve_resource(
        self,
        file_key: str,
        message_id: str,
        res_type: ResType,
        save_dir: str,
        file_name: str = "",
    ) -> str:
        """把飞书消息资源(file_key)下载并上传七牛,返回可访问 URL。

        res_type: 'image' 或 'file'(飞书接口只区分这两种)。
        save_dir: 七牛上的目录名,如 images / files。
        file_name: 原始文件名。非空时:
          - 按扩展名推断 mime_type 写入七牛(修正 Content-Type);
          - URL 追加 ?attname=<urlencode(file_name)>,让七牛返回正确的
            Content-Disposition: attachment; filename=,浏览器即可按正确文件名下载。
        返回七牛可访问 URL;下载/上传失败或未配置则返回空串(调用方保留 file_key)。
        """
        if not file_key:
            return ""

        settings = get_settings()
        if not settings.image_enabled:
            logger.debug("图片资源未启用(缺七牛/飞书配置),跳过 %s=%s", res_type, file_key)
            return ""

        client = self._get_client()
        if client is None:
            logger.warning("飞书客户端不可用,跳过 %s 下载 %s", res_type, file_key)
            return ""

        # 下载飞书资源二进制(需 message_id + file_key + type)
        data = self._download_resource(client, file_key, message_id, res_type)
        if not data:
            logger.warning("下载飞书资源失败 %s=%s message_id=%s", res_type, file_key, message_id)
            return ""

        # 上传到七牛,key 用 file_key(天然唯一,且能对回去原资源)
        key = f"{_PROVIDER}/{save_dir}/{file_key}"
        # 按文件名扩展名推断 MIME 类型,让七牛的 Content-Type 正确(否则默认
        # application/octet-stream,浏览器无法识别文件类型,表现为"打不开/损坏")。
        mime_type = _guess_mime(file_name)
        # 文件/视频类一律走分片断点续传上传:put_data 是单个 POST,大文件
        # (实测 ~25MB 起)会卡在 60s socket 超时(status=-1);分片上传每片
        # 独立计超时,实测稳定。图片小且 put_data 表现良好,保持原路径。
        ok, err = self._upload_to_qiniu(
            key, data, mime_type=mime_type, use_file_api=(save_dir == "files")
        )
        if not ok:
            logger.warning("上传资源到七牛失败 %s=%s: %s", res_type, file_key, err)
            return ""

        url = f"{settings.qiniu_domain.rstrip('/')}/{key}"
        # 文件类型:追加 ?attname= 让七牛返回正确 Content-Disposition,下载文件名带扩展名。
        if file_name:
            url = f"{url}?attname={quote(file_name)}"
        logger.info("资源已落盘(%s): %s -> %s", res_type, file_key, url)
        return url

    # ------------------------------------------------------------ 飞书下载
    def _download_resource(
        self,
        client: lark.Client,
        file_key: str,
        message_id: str,
        res_type: ResType,
    ) -> bytes | None:
        """调飞书[获取消息中的资源文件]下载资源二进制。失败返回 None。

        接口: GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={res_type}
        必须带 message_id,缺 message_id 或走 images 快捷接口都会报 234001。
        """
        try:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(res_type)
                .build()
            )
            resp = client.im.v1.message_resource.get(req)
            if resp is None or not resp.success():
                logger.warning(
                    "获取飞书资源失败 %s=%s message_id=%s: code=%s msg=%s",
                    res_type,
                    file_key,
                    message_id,
                    getattr(resp, "code", "?"),
                    getattr(resp, "msg", "?"),
                )
                return None
            file_io = getattr(resp, "file", None)
            if file_io is None:
                logger.warning("飞书资源响应无内容 %s=%s", res_type, file_key)
                return None
            try:
                return file_io.read()
            except Exception as e:  # noqa: BLE001
                logger.warning("读取飞书资源字节失败 %s=%s: %s", res_type, file_key, e)
                return None
        except Exception as e:  # noqa: BLE001 - 网络/权限等统一降级
            logger.warning("下载飞书资源异常 %s=%s: %s", res_type, file_key, e)
            return None

    # ------------------------------------------------------------ 七牛上传
    # 七牛 put_data(表单上传)上限 100MB,超过须改用分片/断点续传上传。
    _CHUNK_THRESHOLD = 100 * 1024 * 1024
    # 网络层失败(status=-1)等自动重试次数
    _UPLOAD_RETRIES = 3

    def _upload_to_qiniu(
        self,
        key: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        use_file_api: bool = False,
    ) -> tuple[bool, str]:
        """把字节上传到七牛。返回 (是否成功, 出错信息)。

        - use_file_api=True(文件/视频)或大文件(>100MB):落临时文件走分片/
          断点续传上传;
        - 其余(图片等小资源):put_data 表单上传;
        - 网络层失败自动重试,避免偶发抖动丢资源。
        """
        try:
            from qiniu import Auth

            settings = get_settings()
            auth = Auth(settings.qiniu_access_key, settings.qiniu_secret_key)
            token = auth.upload_token(settings.qiniu_bucket, key)

            last_err = "unknown"
            for attempt in range(1, self._UPLOAD_RETRIES + 1):
                ok, last_err = self._do_upload(token, key, data, mime_type, use_file_api)
                if ok:
                    return True, ""
                logger.warning(
                    "七牛上传第 %d/%d 次失败 key=%s: %s",
                    attempt, self._UPLOAD_RETRIES, key, last_err,
                )
            return False, last_err
        except Exception as e:  # noqa: BLE001
            logger.warning("七牛上传异常 key=%s: %s", key, e)
            return False, str(e)

    def _do_upload(
        self, token: str, key: str, data: bytes, mime_type: str, use_file_api: bool = False
    ) -> tuple[bool, str]:
        """单次上传。返回 (是否成功, 出错信息)。"""
        if use_file_api or len(data) > self._CHUNK_THRESHOLD:
            ret, info = self._put_large(token, key, data, mime_type)
        else:
            from qiniu import put_data

            ret, info = put_data(token, key, data=data, mime_type=mime_type)
        if info is not None and info.status_code == 200:
            return True, ""
        return False, _qiniu_info_message(info)

    def _put_large(
        self, token: str, key: str, data: bytes, mime_type: str
    ) -> tuple:
        """大文件断点续传上传:写临时文件后调 SDK 分片上传。返回 (ret, info)。

        兼容新旧 qiniu SDK:7.x 提供 put_file/put_file_v2,旧版提供 put_chunked。
        """
        import qiniu as _qiniu

        fd, tmp_path = tempfile.mkstemp(prefix="qiniu_", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            for name in ("put_file_v2", "put_file", "put_chunked"):
                fn = getattr(_qiniu, name, None)
                if fn is None:
                    continue
                try:
                    return fn(token, key, tmp_path, mime_type=mime_type)
                except TypeError:
                    # 个别版本无 mime_type 参数
                    return fn(token, key, tmp_path)
            raise RuntimeError("qiniu SDK 无可用分片上传函数")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

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


def _guess_mime(file_name: str) -> str:
    """按文件名扩展名推断 MIME 类型;无法推断时回退 octet-stream。

    飞书上传的普通文件(如 .b / .txt)若不设正确 Content-Type,七牛默认返回
    application/octet-stream,浏览器无法识别,表现为文件打不开/损坏。
    """
    if not file_name:
        return "application/octet-stream"
    mime, _enc = mimetypes.guess_type(file_name)
    return mime or "application/octet-stream"


@lru_cache
def get_image_service() -> ImageService:
    """取资源服务单例。"""
    return ImageService()
