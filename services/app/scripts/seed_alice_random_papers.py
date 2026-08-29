from __future__ import annotations

import asyncio
import random
import uuid

from sqlalchemy import select

from backend.app.audit import record_audit_event
from backend.app.database import migration_session_factory
from backend.app.models import (
    CanonicalIdentifier,
    CanonicalMetadata,
    CanonicalPaper,
    ExternalIdentity,
    Library,
    LibraryItem,
    LibraryMembership,
    Principal,
    WebSession,
)

TITLE_WORDS = (
    "adaptive",
    "analysis",
    "attention",
    "benchmark",
    "causal",
    "clinical",
    "contrastive",
    "data",
    "discovery",
    "efficient",
    "evaluation",
    "foundation",
    "graph",
    "inference",
    "language",
    "learning",
    "multimodal",
    "neural",
    "retrieval",
    "robust",
    "scientific",
    "semantic",
    "systems",
    "uncertainty",
)
VENUES = (
    "Journal of Synthetic Research",
    "Transactions on Machine Intelligence",
    "Proceedings of the Demo Science Conference",
    "Computational Methods Letters",
    "Open Research Benchmarks",
)
AUTHOR_NAMES = (
    "A. Rivera",
    "B. Chen",
    "C. Okafor",
    "D. Singh",
    "E. Müller",
    "F. Nakamura",
    "G. Martin",
    "H. Silva",
)


async def main() -> None:
    rng = random.Random()
    batch_id = uuid.uuid4().hex[:10]
    async with migration_session_factory() as session:
        identity = await session.scalar(
            select(ExternalIdentity)
            .join(WebSession, WebSession.principal_id == ExternalIdentity.principal_id)
            .where(
                ExternalIdentity.email == "alice@example.test",
                WebSession.revoked_at.is_(None),
            )
            .order_by(WebSession.updated_at.desc())
            .limit(1)
        )
        if identity is None:
            raise RuntimeError("No Alice identity with an active browser session was found")
        principal = await session.get(Principal, identity.principal_id)
        library = await session.scalar(
            select(Library)
            .join(LibraryMembership, LibraryMembership.library_id == Library.library_id)
            .where(
                LibraryMembership.principal_id == identity.principal_id,
                LibraryMembership.status == "ACTIVE",
                Library.library_type == "PERSONAL",
                Library.status == "ACTIVE",
            )
            .limit(1)
        )
        if principal is None or library is None:
            raise RuntimeError("Alice's active Personal Library was not found")

        for index in range(1, 41):
            paper = CanonicalPaper(status="ACTIVE")
            session.add(paper)
            await session.flush()
            words = rng.sample(TITLE_WORDS, rng.randint(4, 7))
            title = " ".join(words).title() + f": Study {index:02d}"
            metadata = CanonicalMetadata(
                canonical_paper_id=paper.canonical_paper_id,
                metadata_source="UNDEFINED",
                title=title,
                abstract=(
                    "Synthetic catalogue record generated for pagination, filtering, and bulk "
                    "organization acceptance testing."
                ),
                publication_year=rng.randint(2012, 2026),
                venue=rng.choice(VENUES),
                authors=[
                    {"name": name}
                    for name in rng.sample(AUTHOR_NAMES, rng.randint(1, 4))
                ],
                extra={"fixture": "alice-random-40", "batch_id": batch_id},
                provenance={"source": "development_seed"},
                revision=1,
                updated_by=principal.principal_id,
            )
            session.add(metadata)
            doi = f"10.9998/alice-{batch_id}-{index:02d}"
            session.add(
                CanonicalIdentifier(
                    canonical_paper_id=paper.canonical_paper_id,
                    scheme="DOI",
                    normalized_value=doi,
                    original_value=doi,
                )
            )
            session.add(
                LibraryItem(
                    library_id=library.library_id,
                    canonical_paper_id=paper.canonical_paper_id,
                    item_type="PAPER",
                    status="ACTIVE",
                    local_overrides={},
                    revision=1,
                    saved_by=principal.principal_id,
                )
            )

        record_audit_event(
            session,
            "development.random_papers_seeded",
            actor_principal_id=principal.principal_id,
            library_id=library.library_id,
            details={"batch_id": batch_id, "count": 40},
        )
        await session.commit()
        print(
            f"Seeded 40 papers into {library.name} "
            f"(library_id={library.library_id}, batch_id={batch_id})."
        )


if __name__ == "__main__":
    asyncio.run(main())
