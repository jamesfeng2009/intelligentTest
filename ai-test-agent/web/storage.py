"""对象存储抽象（对齐任务清单 T39）。

- 默认 LocalStorage：产物落盘到 data/storage/（零依赖，演示即用）
- 配置 MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY 后自动启用 MinioStorage（生产模式）
- 两类存储返回可公开访问的 URL 供前端下载/预览
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol

_STORAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "storage"


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes | None: ...
    def list(self, prefix: str = "") -> list[str]: ...
    def url(self, key: str) -> str: ...


class LocalStorage:
    """本地文件实现：data/storage/<key>，URL 走 /storage/<key> 静态路由。"""

    def __init__(self, root: Path = _STORAGE_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes) -> str:
        safe = key.lstrip("/").replace("..", "_")
        target = self.root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return safe

    def get(self, key: str) -> bytes | None:
        target = self.root / key.lstrip("/")
        if target.is_file():
            return target.read_bytes()
        return None

    def list(self, prefix: str = "") -> list[str]:
        base = self.root / prefix if prefix else self.root
        return [str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file()]

    def url(self, key: str) -> str:
        return f"/storage/{key}"


class MinioStorage:
    """MinIO 实现（生产模式）。依赖 minio 包，未安装时给出明确提示。"""

    def __init__(self) -> None:
        try:
            from minio import Minio  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("生产存储需要 pip install minio") from e
        endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
        access = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
        secret = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
        self._bucket = os.environ.get("MINIO_BUCKET", "ai-test")
        self._client = Minio(endpoint, access_key=access, secret_key=secret, secure=False)
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
        self._endpoint = f"http://{endpoint}"

    def put(self, key: str, data: bytes) -> str:
        import io

        safe = key.lstrip("/")
        self._client.put_object(self._bucket, safe, io.BytesIO(data), len(data))
        return safe

    def get(self, key: str) -> bytes | None:
        resp = self._client.get_object(self._bucket, key.lstrip("/"))
        return resp.read()

    def list(self, prefix: str = "") -> list[str]:
        return [o.object_name for o in self._client.list_objects(self._bucket, prefix=prefix)]

    def url(self, key: str) -> str:
        return f"{self._endpoint}/{self._bucket}/{key}"


def create_storage() -> Storage:
    if os.environ.get("MINIO_ENDPOINT"):
        return MinioStorage()
    return LocalStorage()


def random_key(prefix: str, suffix: str = "") -> str:
    return f"{prefix}/{uuid.uuid4().hex[:12]}{suffix}"


def copy_file_to_storage(storage: Storage, src: Path, prefix: str = "artifacts") -> str:
    """把本地产物文件写入存储，返回可访问 key。"""
    if not src.is_file():
        return ""
    return storage.put(random_key(prefix, src.suffix), src.read_bytes())
