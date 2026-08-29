"""Attach three readable demo Pipeline Documents to Alice's first two items."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from backend.app.assets.service import artifact_service
from backend.app.assets.service_blob import blob_service
from backend.app.assets.storage import get_object_storage
from backend.app.database import migration_engine, migration_session_factory
from backend.app.models import ExternalIdentity, Library, LibraryItem, LibraryMembership


async def main() -> None:
    async with migration_session_factory() as session:
        row = (
            await session.execute(
                select(ExternalIdentity.principal_id, Library.library_id)
                .join(
                    LibraryMembership,
                    LibraryMembership.principal_id == ExternalIdentity.principal_id,
                )
                .join(Library, Library.library_id == LibraryMembership.library_id)
                .where(
                    ExternalIdentity.email == "alice@example.test",
                    LibraryMembership.status == "ACTIVE",
                    Library.library_type == "PERSONAL",
                )
            )
        ).first()
        if row is None:
            raise RuntimeError("Alice's personal Library is not initialized")
        principal_id, library_id = row
        items = list(
            await session.scalars(
                select(LibraryItem)
                .where(
                    LibraryItem.library_id == library_id,
                    LibraryItem.status == "ACTIVE",
                )
                .order_by(LibraryItem.created_at.desc(), LibraryItem.library_item_id.desc())
                .limit(2)
            )
        )
        if len(items) < 2:
            raise RuntimeError("Alice needs at least two active Library Items")

        documents = (
            (
                items[0],
                "document:fold-demo-technical",
                "Technical synthesis.md",
                "# Technical synthesis\n\nA demo Pipeline Document for folding acceptance.\n",
            ),
            (
                items[0],
                "document:fold-demo-critical",
                "Critical review.md",
                "# Critical review\n\nA second demo Document under the same item.\n",
            ),
            (
                items[1],
                "document:fold-demo-planning",
                "Planning architecture notes.md",
                "# Planning architecture notes\n\nA demo Document for the second item.\n",
            ),
        )
        storage = get_object_storage()
        await storage.ensure_bucket()
        for item, artifact_key, filename, markdown in documents:
            blob = await blob_service.store_bytes(
                session,
                storage,
                data=markdown.encode("utf-8"),
                media_type="text/markdown",
                actor_principal_id=principal_id,
            )
            await artifact_service.set_canonical(
                session,
                canonical_paper_id=item.canonical_paper_id,
                artifact_key=artifact_key,
                artifact_type="PIPELINE_DOCUMENT",
                blob_id=blob.blob_id,
                media_type="text/markdown",
                actor_principal_id=principal_id,
                original_filename=filename,
                provenance={"source": "development_document_fold_seed"},
            )
        await session.commit()
        print(
            "Seeded Documents:",
            f"first_item={items[0].library_item_id}:2",
            f"second_item={items[1].library_item_id}:1",
        )


async def run() -> None:
    try:
        await main()
    finally:
        await migration_engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
