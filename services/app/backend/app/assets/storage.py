from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import BinaryIO, Protocol
from urllib.parse import urlparse

from minio import Minio
from minio.commonconfig import CopySource

from ..config import get_settings


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    byte_size: int
    etag: str | None
    content_type: str | None


class ObjectStorage(Protocol):
    async def ensure_bucket(self) -> None: ...

    async def put(
        self, key: str, stream: BinaryIO, byte_size: int, media_type: str
    ) -> StoredObject: ...

    async def promote(self, staging_key: str, content_key: str) -> StoredObject: ...

    async def stat(self, key: str) -> StoredObject | None: ...

    async def delete(self, key: str) -> None: ...

    async def read_bytes(self, key: str, max_bytes: int) -> bytes: ...

    async def presigned_get(self, key: str, expires: timedelta) -> str: ...


class MinioObjectStorage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        public_endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
    ) -> None:
        self.bucket = bucket
        self._client = self._client_for(
            endpoint_url, access_key=access_key, secret_key=secret_key, region=region
        )
        self._public_client = self._client_for(
            public_endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
        )

    @staticmethod
    def _client_for(endpoint_url: str, *, access_key: str, secret_key: str, region: str) -> Minio:
        parsed = urlparse(endpoint_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("S3 endpoint must be an absolute HTTP(S) URL")
        if parsed.path not in {"", "/"}:
            raise ValueError("S3 endpoint must not include a path")
        return Minio(
            parsed.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
            region=region,
        )

    async def ensure_bucket(self) -> None:
        def ensure() -> None:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)

        await asyncio.to_thread(ensure)

    async def put(
        self, key: str, stream: BinaryIO, byte_size: int, media_type: str
    ) -> StoredObject:
        result = await asyncio.to_thread(
            self._client.put_object,
            self.bucket,
            key,
            stream,
            byte_size,
            content_type=media_type,
        )
        return StoredObject(self.bucket, key, byte_size, result.etag, media_type)

    async def promote(self, staging_key: str, content_key: str) -> StoredObject:
        existing = await self.stat(content_key)
        if existing is None:
            await asyncio.to_thread(
                self._client.copy_object,
                self.bucket,
                content_key,
                CopySource(self.bucket, staging_key),
            )
            existing = await self.stat(content_key)
            if existing is None:
                raise RuntimeError("Object promotion completed without a destination object")
        await self.delete(staging_key)
        return existing

    async def stat(self, key: str) -> StoredObject | None:
        from minio.error import S3Error

        try:
            value = await asyncio.to_thread(self._client.stat_object, self.bucket, key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return None
            raise
        if value.size is None:
            raise RuntimeError("Object storage returned an object without a size")
        return StoredObject(
            self.bucket,
            key,
            int(value.size),
            value.etag,
            value.content_type,
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.remove_object, self.bucket, key)

    async def read_bytes(self, key: str, max_bytes: int) -> bytes:
        def read() -> bytes:
            response = self._client.get_object(self.bucket, key)
            try:
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError("Stored object exceeds the processing limit")
                return data
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(read)

    async def presigned_get(self, key: str, expires: timedelta) -> str:
        return await asyncio.to_thread(
            self._public_client.presigned_get_object,
            self.bucket,
            key,
            expires=expires,
        )

    @staticmethod
    def staging_key() -> str:
        return f"staging/{uuid.uuid4()}"

    @staticmethod
    def content_key(sha256: str) -> str:
        normalized = sha256.lower()
        if len(normalized) != 64 or any(value not in "0123456789abcdef" for value in normalized):
            raise ValueError("SHA-256 must contain 64 hexadecimal characters")
        return f"blobs/sha256/{normalized[:2]}/{normalized[2:4]}/{normalized}"


@lru_cache
def get_object_storage() -> MinioObjectStorage:
    settings = get_settings()
    return MinioObjectStorage(
        endpoint_url=settings.s3_endpoint_url,
        public_endpoint_url=settings.s3_public_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        bucket=settings.s3_bucket,
        region=settings.s3_region,
    )
