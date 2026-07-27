# -*- coding: utf-8 -*-
"""腾讯云 COS 成片存储（可选）。

用途：把生成成片（如换装/换背景视频）上传到 COS，前端用 COS/CDN 直链播放下载，
省服务器磁盘、更持久。**所有密钥走环境变量，绝不进 git。** 配置不全时自动禁用、
调用方回退本地链接，因此本模块对现有流程零影响。

需要的环境变量（写在服务器 EnvironmentFile，如 /home/ubuntu/content-api/content.env）：
    COS_SECRET_ID      腾讯云 SecretId
    COS_SECRET_KEY     腾讯云 SecretKey（机密）
    COS_REGION         地域，如 ap-guangzhou
    COS_BUCKET         完整桶名（带 APPID 后缀），如 xxx-1250000000
可选：
    COS_PREFIX         对象键前缀，如 huangque/   （默认空）
    COS_DOMAIN         自定义/CDN 访问域名，如 https://cdn.example.com （默认用 myqcloud 默认域名）
    COS_PUBLIC         桶是否公有读：1(默认)=返回直链；0=私有，返回带签名的临时链接
    COS_SIGN_EXPIRE    私有读签名有效期(秒)，默认 604800(7天)
    COS_DELETE_LOCAL   上传成功后是否删除本地成片：0(默认)=保留(可回退)；1=删除省磁盘

服务器前置：`pip install cos-python-sdk-v5`（提供 qcloud_cos）。
"""
import os
import pathlib
import tempfile
import urllib.parse
import urllib.request

_SECRET_ID   = os.environ.get("COS_SECRET_ID", "").strip()
_SECRET_KEY  = os.environ.get("COS_SECRET_KEY", "").strip()
_REGION      = os.environ.get("COS_REGION", "").strip()
_BUCKET      = os.environ.get("COS_BUCKET", "").strip()
_PREFIX      = os.environ.get("COS_PREFIX", "").strip().strip("/")
_DOMAIN      = os.environ.get("COS_DOMAIN", "").strip().rstrip("/")
_PUBLIC      = os.environ.get("COS_PUBLIC", "1").strip().lower() not in ("0", "false", "no", "")
_SIGN_EXPIRE = int(os.environ.get("COS_SIGN_EXPIRE", "604800") or 604800)
_DELETE_LOCAL = os.environ.get("COS_DELETE_LOCAL", "0").strip().lower() in ("1", "true", "yes")

_client_singleton = None


def enabled():
    """四个必填项齐全才启用 COS。"""
    return bool(_SECRET_ID and _SECRET_KEY and _REGION and _BUCKET)


def delete_local_after_upload():
    return _DELETE_LOCAL


def _client():
    global _client_singleton
    if _client_singleton is None:
        from qcloud_cos import CosConfig, CosS3Client  # 服务器 pip 装；本地/CI 不触发 import
        cfg = CosConfig(Region=_REGION, SecretId=_SECRET_ID, SecretKey=_SECRET_KEY, Scheme="https")
        _client_singleton = CosS3Client(cfg)
    return _client_singleton


def _object_key(rel):
    rel = str(rel).lstrip("/")
    return (_PREFIX + "/" + rel) if _PREFIX else rel


def _url(full_key, private=False):
    if private:
        return _client().get_presigned_url(Method="GET", Bucket=_BUCKET, Key=full_key, Expired=_SIGN_EXPIRE)
    if _DOMAIN:
        return _DOMAIN + "/" + full_key
    if _PUBLIC:
        return "https://%s.cos.%s.myqcloud.com/%s" % (_BUCKET, _REGION, full_key)
    # 私有读：返回带签名的临时链接（会过期，故推荐公有读或挂 CDN）
    return _client().get_presigned_url(Method="GET", Bucket=_BUCKET, Key=full_key, Expired=_SIGN_EXPIRE)


def object_url(rel_key, private=False):
    """为已上传对象生成访问地址；私有对象每次调用都会刷新短期签名。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    return _url(_object_key(rel_key), private=private)


def create_presigned_put(rel_key, content_type, expires=900):
    """生成仅允许上传一个指定对象的短时 PUT URL。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        raise ValueError("上传签名有效期无效")
    if not 60 <= expires <= 3600:
        raise ValueError("上传签名有效期必须在60到3600秒之间")
    content_type = str(content_type or "").strip().lower()
    if not content_type or "\r" in content_type or "\n" in content_type:
        raise ValueError("上传文件类型无效")
    return _client().get_presigned_url(
        Method="PUT",
        Bucket=_BUCKET,
        Key=_object_key(rel_key),
        Expired=expires,
        Headers={"Content-Type": content_type},
    )


def head(rel_key):
    """读取对象大小和类型；调用方据此完成直传确认。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    response = _client().head_object(Bucket=_BUCKET, Key=_object_key(rel_key))
    raw_size = response.get("Content-Length", response.get("ContentLength", 0))
    return {
        "size_bytes": int(raw_size or 0),
        "content_type": str(
            response.get("Content-Type", response.get("ContentType", "")) or ""
        ).split(";", 1)[0].strip().lower(),
        "etag": str(response.get("ETag") or "").strip('"'),
    }


def get_media_info(rel_key):
    """通过 COS 数据万象读取媒体时长、视频流和音轨信息。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    response = _client().get_media_info(Bucket=_BUCKET, Key=_object_key(rel_key))
    info = response.get("MediaInfo") if isinstance(response, dict) else None
    if not isinstance(info, dict):
        raise RuntimeError("COS 未返回媒体信息")
    return info


def transfer_remote(remote_url, rel_key, max_bytes):
    """有界流式转存供应商 HTTPS 文件，不在内存中缓存整段视频。"""
    parsed = urllib.parse.urlsplit(str(remote_url or ""))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("远程媒体必须使用HTTPS地址")
    try:
        max_bytes = int(max_bytes)
    except (TypeError, ValueError):
        raise ValueError("远程媒体大小限制无效")
    if max_bytes <= 0:
        raise ValueError("远程媒体大小限制无效")

    temp_path = None
    try:
        request = urllib.request.Request(
            remote_url,
            headers={"User-Agent": "HuangqueMediaTransfer/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(
                ";", 1
            )[0].strip().lower()
            suffix = pathlib.PurePosixPath(parsed.path).suffix[:12] or ".bin"
            with tempfile.NamedTemporaryFile(
                prefix="hq-ai-edit-transfer-", suffix=suffix, delete=False
            ) as output:
                temp_path = output.name
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("远程媒体大小超过限制")
                    output.write(chunk)
        return put_file(
            temp_path,
            rel_key,
            content_type or "application/octet-stream",
            private=True,
        )
    finally:
        if temp_path:
            try:
                pathlib.Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass


def upload(local_path, rel_key, content_type=None, private=False):
    """把本地文件上传到 COS，返回可访问 URL。未启用或失败会抛异常，由调用方回退本地。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    full_key = _object_key(rel_key)
    with open(local_path, "rb") as fp:
        kwargs = {"Bucket": _BUCKET, "Key": full_key, "Body": fp}
        if content_type:
            kwargs["ContentType"] = content_type
        if private:
            kwargs["ACL"] = "private"
        _client().put_object(**kwargs)
    return _url(full_key, private=private)


def put_bytes(data, rel_key, content_type=None, private=False):
    """把内存字节直接上传到 COS，返回可访问 URL。用于转存远程 URL(如采集视频 CDN 直链)。
    未启用或失败会抛异常，由调用方回退原链接。"""
    if not enabled():
        raise RuntimeError("COS 未配置")
    full_key = _object_key(rel_key)
    kwargs = {"Bucket": _BUCKET, "Key": full_key, "Body": data}
    if content_type:
        kwargs["ContentType"] = content_type
    if private:
        kwargs["ACL"] = "private"
    _client().put_object(**kwargs)
    return _url(full_key, private=private)


def put_file(path, rel_key, content_type=None, private=False):
    """从磁盘文件上传到 COS，返回可访问 URL。

    采集视频最大 100MB，用 put_bytes 得先把整段读进内存。put_object 接受文件对象，
    SDK 会分块读，内存恒定。未启用或失败会抛异常，由调用方回退原链接。
    """
    if not enabled():
        raise RuntimeError("COS 未配置")
    full_key = _object_key(rel_key)
    with open(path, "rb") as f:
        kwargs = {"Bucket": _BUCKET, "Key": full_key, "Body": f}
        if content_type:
            kwargs["ContentType"] = content_type
        if private:
            kwargs["ACL"] = "private"
        _client().put_object(**kwargs)
    return _url(full_key, private=private)
