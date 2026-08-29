from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import record_audit_event
from backend.app.authorization.dependencies import Actor, membership_for
from backend.app.jobs.service import job_service
from backend.app.models import (
    BackgroundJob,
    CanonicalIdentifier,
    CanonicalMetadata,
    CanonicalPaper,
    LibraryItem,
)

from .identifiers import ScholarlyIdentifier, normalize_arxiv
from .providers import MetadataResolver, ResolvedMetadata, normalize_doi
from .reconcile import IdentifierConflictError, identifier_reconciliation_service

METADATA_REFRESH_JOB = "METADATA_REFRESH"


class MetadataRefreshUnresolved(RuntimeError):
    pass


class MetadataRefreshService:
    async def enqueue_batch(
        self,
        session: AsyncSession,
        actor: Actor,
        library_id: uuid.UUID,
        *,
        library_item_ids: list[uuid.UUID],
        request_id: uuid.UUID,
        refresh_mode: Literal["AUTO", "MANUAL"],
    ) -> list[BackgroundJob]:
        await membership_for(
            session, actor=actor, library_id=library_id, allowed_roles={"OWNER", "EDITOR"}
        )
        unique_ids = list(dict.fromkeys(library_item_ids))
        items = list(
            await session.scalars(
                select(LibraryItem).where(
                    LibraryItem.library_id == library_id,
                    LibraryItem.library_item_id.in_(unique_ids),
                    LibraryItem.status != "PURGED",
                )
            )
        )
        if len(items) != len(unique_ids):
            raise LookupError("One or more Library Items were not found")
        jobs: list[BackgroundJob] = []
        for item_id in unique_ids:
            jobs.append(
                await job_service.enqueue(
                    session,
                    actor,
                    library_id,
                    job_type=METADATA_REFRESH_JOB,
                    payload={
                        "library_item_id": str(item_id),
                        "refresh_mode": refresh_mode,
                    },
                    idempotency_key=f"{request_id}:{item_id}",
                    progress_total=2,
                    max_attempts=2,
                )
            )
        record_audit_event(
            session,
            "library.metadata_refresh_requested",
            actor_principal_id=actor.principal_id,
            library_id=library_id,
            details={
                "request_id": str(request_id),
                "library_item_ids": [str(value) for value in unique_ids],
                "refresh_mode": refresh_mode,
            },
        )
        await session.commit()
        return jobs

    async def execute_claimed(
        self,
        session: AsyncSession,
        job: BackgroundJob,
        *,
        worker_id: str,
        resolver: MetadataResolver,
    ) -> None:
        if job.job_type != METADATA_REFRESH_JOB:
            raise ValueError("Unsupported job type")
        try:
            item_id = uuid.UUID(str(job.payload["library_item_id"]))
        except (KeyError, TypeError, ValueError):
            await job_service.fail(
                session,
                job.job_id,
                worker_id=worker_id,
                error={"code": "INVALID_JOB_PAYLOAD"},
                retry_delay_seconds=0,
            )
            return

        item = await session.scalar(
            select(LibraryItem).where(
                LibraryItem.library_id == job.library_id,
                LibraryItem.library_item_id == item_id,
                LibraryItem.status != "PURGED",
            )
        )
        if item is None:
            await job_service.fail(
                session,
                job.job_id,
                worker_id=worker_id,
                error={"code": "LIBRARY_ITEM_NOT_FOUND"},
                retry_delay_seconds=0,
            )
            return

        identifiers = list(
            await session.scalars(
                select(CanonicalIdentifier).where(
                    CanonicalIdentifier.canonical_paper_id == item.canonical_paper_id,
                    CanonicalIdentifier.scheme.in_({"DOI", "ARXIV"}),
                )
            )
        )
        dois = [value.normalized_value for value in identifiers if value.scheme == "DOI"]
        arxiv_ids = [value.normalized_value for value in identifiers if value.scheme == "ARXIV"]
        formal_doi = next(
            (value for value in dois if not value.startswith("10.48550/arxiv.")), None
        )
        arxiv_id = arxiv_ids[0] if arxiv_ids else None
        if arxiv_id is None:
            arxiv_doi = next((value for value in dois if value.startswith("10.48550/arxiv.")), None)
            if arxiv_doi is not None:
                arxiv_id = normalize_arxiv(arxiv_doi[len("10.48550/arxiv.") :])
        lookup_scheme: Literal["DOI", "ARXIV"]
        if formal_doi is not None:
            lookup_scheme, lookup_value = "DOI", formal_doi
        elif arxiv_id is not None:
            lookup_scheme, lookup_value = "ARXIV", arxiv_id
        elif dois:
            lookup_scheme, lookup_value = "DOI", dois[0]
        else:
            await job_service.fail(
                session,
                job.job_id,
                worker_id=worker_id,
                error={"code": "SCHOLARLY_IDENTIFIER_NOT_FOUND"},
                retry_delay_seconds=0,
            )
            return

        current_metadata = await session.get(CanonicalMetadata, item.canonical_paper_id)
        if (
            str(job.payload.get("refresh_mode") or "MANUAL") == "AUTO"
            and current_metadata is not None
            and current_metadata.metadata_source in {"CROSSREF", "OPENALEX", "ARXIV", "ZOTERO"}
        ):
            await job_service.succeed(
                session,
                job.job_id,
                worker_id=worker_id,
                result={
                    "library_item_id": str(item_id),
                    "metadata_source": current_metadata.metadata_source,
                    "identifier": lookup_value,
                    "skipped": "CANONICAL_METADATA_ALREADY_RESOLVED",
                },
            )
            return

        await job_service.progress(
            session,
            job.job_id,
            worker_id=worker_id,
            current=1,
            total=2,
            message=f"Resolving {lookup_scheme} metadata",
        )
        resolved = await resolver.resolve(lookup_value, scheme=lookup_scheme)
        if resolved is None:
            await job_service.fail(
                session,
                job.job_id,
                worker_id=worker_id,
                error={
                    "code": "METADATA_UNRESOLVED",
                    "scheme": lookup_scheme,
                    "identifier": lookup_value,
                },
                retry_delay_seconds=0,
            )
            return

        reconciliation_values = [
            ScholarlyIdentifier(
                scheme=value.scheme,
                normalized_value=value.normalized_value,
                original_value=value.original_value,
                evidence="EXISTING_CANONICAL",
            )
            for value in identifiers
        ]
        discovered = (("DOI", resolved.doi), *resolved.related_identifiers)
        known = {(value.scheme, value.normalized_value) for value in reconciliation_values}
        for scheme, original in discovered:
            normalized = normalize_doi(original) if scheme == "DOI" else normalize_arxiv(original)
            if (scheme, normalized) not in known:
                reconciliation_values.append(
                    ScholarlyIdentifier(
                        scheme=scheme,
                        normalized_value=normalized,
                        original_value=original,
                        evidence=resolved.source,
                    )
                )
                known.add((scheme, normalized))
        try:
            reconciled = await identifier_reconciliation_service.reconcile_identifiers(
                session,
                library_id=job.library_id,
                library_item_id=item_id,
                identifiers=tuple(reconciliation_values),
                actor_principal_id=job.actor_principal_id,
            )
        except IdentifierConflictError:
            await job_service.fail(
                session,
                job.job_id,
                worker_id=worker_id,
                error={"code": "IDENTIFIER_CONFLICT"},
                retry_delay_seconds=0,
            )
            return

        await self.apply_resolved(
            session,
            canonical_paper_id=reconciled.canonical_paper_id,
            resolved=resolved,
            actor_principal_id=job.actor_principal_id,
        )
        await job_service.succeed(
            session,
            job.job_id,
            worker_id=worker_id,
            result={
                "library_item_id": str(reconciled.library_item_id),
                "metadata_source": resolved.source,
                "doi": resolved.doi,
            },
        )

    @staticmethod
    async def apply_resolved(
        session: AsyncSession,
        *,
        canonical_paper_id: uuid.UUID,
        resolved: ResolvedMetadata,
        actor_principal_id: uuid.UUID | None,
    ) -> CanonicalMetadata:
        paper = await session.scalar(
            select(CanonicalPaper)
            .where(CanonicalPaper.canonical_paper_id == canonical_paper_id)
            .with_for_update()
        )
        if paper is None or paper.status != "ACTIVE":
            raise MetadataRefreshUnresolved("Canonical Paper is unavailable")
        metadata = await session.get(CanonicalMetadata, canonical_paper_id)
        if metadata is None:
            raise MetadataRefreshUnresolved("Current metadata is unavailable")

        metadata.metadata_source = resolved.source
        metadata.source_record_id = resolved.source_record_id
        metadata.title = resolved.title
        metadata.abstract = resolved.abstract
        metadata.publication_year = resolved.publication_year
        metadata.publication_month = resolved.publication_month
        metadata.publication_day = resolved.publication_day
        metadata.publication_date = resolved.publication_date
        metadata.publication_date_precision = resolved.publication_date_precision
        metadata.work_type = resolved.work_type
        metadata.venue = resolved.venue
        metadata.canonical_url = resolved.canonical_url
        metadata.publisher = resolved.publisher
        metadata.volume = resolved.volume
        metadata.issue = resolved.issue
        metadata.pages = resolved.pages
        metadata.article_number = resolved.article_number
        metadata.language = resolved.language
        metadata.issn = resolved.issn
        metadata.isbn = resolved.isbn
        metadata.authors = resolved.authors
        metadata.extra = resolved.extra
        metadata.provenance = {
            "source": resolved.source,
            "source_record_id": resolved.source_record_id,
            "doi": resolved.doi,
            "retrieved_at": datetime.now(UTC).isoformat(),
        }
        metadata.revision += 1
        metadata.updated_by = actor_principal_id
        await session.flush()
        return metadata


metadata_refresh_service = MetadataRefreshService()
