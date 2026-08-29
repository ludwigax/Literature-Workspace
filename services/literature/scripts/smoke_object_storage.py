from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import timedelta
from io import BytesIO

from backend.app.assets.storage import get_object_storage


async def main() -> None:
    storage = get_object_storage()
    await storage.ensure_bucket()
    content = f"literature-v2-storage-smoke:{uuid.uuid4()}".encode()
    digest = hashlib.sha256(content).hexdigest()
    staging_key = storage.staging_key()
    content_key = storage.content_key(digest)
    await storage.put(staging_key, BytesIO(content), len(content), "application/octet-stream")
    promoted = await storage.promote(staging_key, content_key)
    assert promoted.byte_size == len(content)
    assert await storage.stat(staging_key) is None
    assert await storage.stat(content_key) is not None
    url = await storage.presigned_get(content_key, timedelta(minutes=1))
    assert content_key in url
    await storage.delete(content_key)
    assert await storage.stat(content_key) is None
    print("Object storage smoke passed: stage -> promote -> sign -> delete")


if __name__ == "__main__":
    asyncio.run(main())
