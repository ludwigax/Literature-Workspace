from __future__ import annotations

import hashlib
import io
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Blob

from .storage import ObjectStorage


class BlobService:
    async def store_bytes(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        *,
        data: bytes,
        media_type: str,
        actor_principal_id: uuid.UUID | None,
    ) -> Blob:
        sha256 = hashlib.sha256(data).hexdigest()
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"blob:{sha256}"},
        )
        existing = await session.scalar(select(Blob).where(Blob.sha256 == sha256))
        if existing is not None and existing.status == "AVAILABLE":
            stored = await storage.stat(existing.storage_key)
            if stored is not None and stored.byte_size == len(data):
                return existing

        staging_key = f"staging/{uuid.uuid4()}"
        content_key = f"blobs/sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"
        await storage.put(staging_key, io.BytesIO(data), len(data), media_type)
        stored = await storage.promote(staging_key, content_key)
        if existing is None:
            existing = Blob(
                sha256=sha256,
                byte_size=len(data),
                media_type=media_type,
                storage_bucket=stored.bucket,
                storage_key=stored.key,
                status="AVAILABLE",
                created_by=actor_principal_id,
            )
            session.add(existing)
        else:
            existing.byte_size = len(data)
            existing.media_type = media_type
            existing.storage_bucket = stored.bucket
            existing.storage_key = stored.key
            existing.status = "AVAILABLE"
        await session.flush()
        return existing


blob_service = BlobService()
