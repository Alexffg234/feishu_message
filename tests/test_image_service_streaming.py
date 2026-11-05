"""H5 Part2 媒体资源下载/上传流式化测试。

验证:
1. _download_resource 分块流式写入临时文件(不整包读进内存),返回文件路径。
2. _resolve_resource 上传成功后,临时文件在 finally 中被清理(不留脏文件)。
3. _upload_to_qiniu 网络层失败自动重试 3 次,最后返回最后一次错误。
4. _do_upload 对大文件/文件类走 _put_large(从文件路径分片),小图片走 put_file。
5. 上传全程从文件路径读取,不再把整包 bytes 传入。

离线:qiniu 未安装在测试环境(生产侧延迟导入),这里用 sys.modules 注入桩模块,
下载用假 lark client / 假 file 流,不触网。
"""
from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.image_service import ImageService


def _fake_settings():
    return types.SimpleNamespace(
        image_enabled=True,
        qiniu_domain="https://cdn.example.com",
        qiniu_access_key="ak",
        qiniu_secret_key="sk",
        qiniu_bucket="bucket",
    )


class _ChunkReader(io.BytesIO):
    """记录每次 read(n) 的请求大小,验证确实按块读取。"""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1):
        self.read_sizes.append(size)
        return super().read(size)


class _SuccessResp:
    def __init__(self, data: bytes):
        self._data = data
        self._reader = _ChunkReader(data)

    def success(self) -> bool:
        return True

    @property
    def file(self):
        return self._reader


class _FailResp:
    def success(self) -> bool:
        return False

    code = 999999
    msg = "boom"


class _NoFileResp:
    def success(self) -> bool:
        return True

    @property
    def file(self):
        return None


class _FakeLarkClient:
    def __init__(self, resp):
        self._resp = resp
        # 与真实 lark client 层级一致:client.im.v1.message_resource.get(req)
        self.im = types.SimpleNamespace(
            v1=types.SimpleNamespace(
                message_resource=types.SimpleNamespace(get=lambda req: self._resp)
            )
        )


def _stub_qiniu(monkeypatch):
    """往 sys.modules 注入一个最小 qiniu 桩,让延迟导入可用。"""
    q = types.ModuleType("qiniu")

    class _Auth:
        def __init__(self, ak, sk):
            pass

        def upload_token(self, bucket, key):
            return "upload-token"

    q.Auth = _Auth

    def _ok_put_file(token, key, path, mime_type="application/octet-stream", **kw):
        return ({"key": key}, types.SimpleNamespace(status_code=200, text_body=""))

    q.put_file = _ok_put_file
    monkeypatch.setitem(sys.modules, "qiniu", q)
    return q


# ------------------------------------------------------------ 下载流式
def test_download_resource_streams_chunks_to_temp_file(tmp_path):
    svc = ImageService(_FakeLarkClient(_SuccessResp(b"abcdefghij")))
    svc._STREAM_CHUNK = 3  # 小分块,强制多次 read

    path = svc._download_resource(svc._client, "k1", "om1", "image")
    try:
        assert path and os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read() == b"abcdefghij"
        # 确实按块读取:存在多次 size==3 的 read,而非一次读完
        sizes = svc._client._resp._reader.read_sizes
        assert len(sizes) >= 4
        assert all(s == 3 for s in sizes)
    finally:
        if path:
            os.remove(path)


def test_download_resource_none_when_resp_fails(tmp_path):
    svc = ImageService(_FakeLarkClient(_FailResp()))
    assert svc._download_resource(svc._client, "k1", "om1", "image") is None


def test_download_resource_none_when_no_file(tmp_path):
    svc = ImageService(_FakeLarkClient(_NoFileResp()))
    assert svc._download_resource(svc._client, "k1", "om1", "image") is None


# ------------------------------------------------------------ resolve + 清理
def test_resolve_resource_cleans_temp_file_and_returns_url(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.image_service.get_settings", lambda: _fake_settings())
    svc = ImageService(_FakeLarkClient(_SuccessResp(b"imgbytes")))

    captured = {}

    def fake_upload(key, src_path, mime_type="application/octet-stream", use_file_api=False):
        captured["src_path"] = src_path
        captured["key"] = key
        captured["use_file_api"] = use_file_api
        assert os.path.exists(src_path)  # 上传前临时文件确实存在
        return True, ""

    monkeypatch.setattr(svc, "_upload_to_qiniu", fake_upload)

    url = svc.resolve_image_url("k1", "om1")
    assert url == "https://cdn.example.com/qiniu/images/k1"
    # 上传后 finally 清理:临时文件已被删除
    assert not os.path.exists(captured["src_path"])
    assert captured["key"] == "qiniu/images/k1"
    assert captured["use_file_api"] is False


def test_resolve_resource_cleans_temp_file_on_upload_fail(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.image_service.get_settings", lambda: _fake_settings())
    svc = ImageService(_FakeLarkClient(_SuccessResp(b"imgbytes")))

    captured = {}

    def fake_upload(key, src_path, mime_type="application/octet-stream", use_file_api=False):
        captured["src_path"] = src_path
        return False, "upload failed"

    monkeypatch.setattr(svc, "_upload_to_qiniu", fake_upload)

    assert svc.resolve_image_url("k1", "om1") == ""
    assert not os.path.exists(captured["src_path"])  # 失败也不残留临时文件


# ------------------------------------------------------------ 上传重试与路径选择
def test_upload_to_qiniu_retries_then_returns_last_err(tmp_path, monkeypatch):
    _stub_qiniu(monkeypatch)
    monkeypatch.setattr("app.services.image_service.get_settings", lambda: _fake_settings())
    svc = ImageService()

    calls = {"n": 0}

    def fail_do_upload(token, key, src_path, mime_type, use_file_api=False):
        calls["n"] += 1
        return False, "network-error"

    monkeypatch.setattr(svc, "_do_upload", fail_do_upload)

    tmp = tmp_path / "a.part"
    tmp.write_bytes(b"x" * 10)
    ok, err = svc._upload_to_qiniu("key1", str(tmp), mime_type="image/png")
    assert ok is False
    assert err == "network-error"
    assert calls["n"] == 3  # 自动重试 3 次


def test_do_upload_uses_put_large_for_file_api(tmp_path, monkeypatch):
    _stub_qiniu(monkeypatch)
    svc = ImageService()

    tmp = tmp_path / "big.part"
    tmp.write_bytes(b"x" * 10)

    put_file_called = {"n": 0}
    large_path = {}

    def fake_put_file(*a, **k):
        put_file_called["n"] += 1
        return ({"key": "k"}, types.SimpleNamespace(status_code=200, text_body=""))

    def fake_put_large(token, key, src_path, mime_type):
        large_path["p"] = src_path
        return ({"key": key}, types.SimpleNamespace(status_code=200, text_body=""))

    monkeypatch.setattr(svc, "_put_large", fake_put_large)
    monkeypatch.setattr("qiniu.put_file", fake_put_file)

    ok, err = svc._do_upload("token", "k", str(tmp), "video/mp4", use_file_api=True)
    assert ok is True
    assert err == ""
    assert large_path["p"] == str(tmp)  # 从文件路径取,而非 bytes
    assert put_file_called["n"] == 0  # 文件类不落 put_file


def test_do_upload_uses_put_file_for_small_image(tmp_path, monkeypatch):
    _stub_qiniu(monkeypatch)
    svc = ImageService()

    tmp = tmp_path / "small.part"
    tmp.write_bytes(b"x" * 10)

    used = {}

    def fake_put_file(token, key, src_path, mime_type="application/octet-stream", **kw):
        used["path"] = src_path
        return ({"key": key}, types.SimpleNamespace(status_code=200, text_body=""))

    monkeypatch.setattr("qiniu.put_file", fake_put_file)

    def fail_put_large(*a, **k):
        raise AssertionError("小文件不应走分片上传")

    monkeypatch.setattr(svc, "_put_large", fail_put_large)

    ok, err = svc._do_upload("token", "k", str(tmp), "image/png")
    assert ok is True
    assert err == ""
    assert used["path"] == str(tmp)  # 小文件从文件路径 put_file
