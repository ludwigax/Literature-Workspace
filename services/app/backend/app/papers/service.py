from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization.dependencies import Actor
from ..ingestion.identifiers import arxiv_id_from_datacite_doi, normalize_arxiv
from ..ingestion.providers import normalize_doi
from ..models import CanonicalIdentifier, CanonicalMetadata, CanonicalPaper


class PaperService:
    """Operations on the global canonical-paper domain, independent of Library."""

    async def resolve(
        self,
        session: AsyncSession,
        identifiers: list[tuple[str, str, str]],
    ) -> CanonicalPaper | None:
        if not identifiers:
            return None
        matches = list(
            await session.scalars(
                select(CanonicalIdentifier).where(
                    or_(
                        *[
                            (CanonicalIdentifier.scheme == scheme)
                            & (CanonicalIdentifier.normalized_value == normalized)
                            for scheme, normalized, _ in identifiers
                        ]
                    )
                )
            )
        )
        paper_ids = {match.canonical_paper_id for match in matches}
        if len(paper_ids) > 1:
            raise HTTPException(status_code=409, detail="Identifiers resolve to different Papers")
        if not paper_ids:
            return None
        paper = await session.get(CanonicalPaper, next(iter(paper_ids)))
        if paper is None or paper.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Canonical Paper is not active")
        return paper

    async def create(
        self,
        session: AsyncSession,
        actor: Actor,
        *,
        metadata: dict[str, Any],
        identifiers: list[tuple[str, str, str]],
    ) -> CanonicalPaper:
        paper = CanonicalPaper(status="ACTIVE")
        session.add(paper)
        await session.flush()
        session.add(
            CanonicalMetadata(
                canonical_paper_id=paper.canonical_paper_id,
                metadata_source="UNDEFINED",
                title=str(metadata["title"]).strip(),
                abstract=self._optional_text(metadata.get("abstract")),
                publication_year=metadata.get("publication_year"),
                publication_month=metadata.get("publication_month"),
                publication_day=metadata.get("publication_day"),
                publication_date=self._parse_date(metadata.get("publication_date")),
                publication_date_precision=metadata.get("publication_date_precision"),
                work_type=self._optional_text(metadata.get("work_type")),
                venue=self._optional_text(metadata.get("venue")),
                canonical_url=self._optional_text(metadata.get("canonical_url")),
                publisher=self._optional_text(metadata.get("publisher")),
                volume=self._optional_text(metadata.get("volume")),
                issue=self._optional_text(metadata.get("issue")),
                pages=self._optional_text(metadata.get("pages")),
                article_number=self._optional_text(metadata.get("article_number")),
                language=self._optional_text(metadata.get("language")),
                issn=list(metadata.get("issn") or []),
                isbn=list(metadata.get("isbn") or []),
                authors=list(metadata.get("authors") or []),
                extra=dict(metadata.get("extra") or {}),
                provenance=dict(metadata.get("provenance") or {"source": "import"}),
                revision=1,
                updated_by=actor.principal_id,
            )
        )
        for scheme, normalized, original in identifiers:
            session.add(
                CanonicalIdentifier(
                    canonical_paper_id=paper.canonical_paper_id,
                    scheme=scheme,
                    normalized_value=normalized,
                    original_value=original,
                )
            )
        await session.flush()
        return paper

    @staticmethod
    def normalize_identifiers(values: list[dict[str, str]]) -> list[tuple[str, str, str]]:
        result: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for value in values:
            scheme = str(value.get("scheme") or "").strip().upper()
            original = str(value.get("value") or "").strip()
            if scheme not in {"DOI", "PMID", "ARXIV", "ISBN", "OTHER"} or not original:
                raise HTTPException(status_code=422, detail="invalid scholarly identifier")
            normalized = original.casefold()
            if scheme == "DOI":
                try:
                    normalized = normalize_doi(original)
                except ValueError as error:
                    raise HTTPException(status_code=422, detail="invalid DOI") from error
                arxiv_id = arxiv_id_from_datacite_doi(normalized)
                if arxiv_id is not None:
                    normalized = f"10.48550/arxiv.{arxiv_id}"
                    arxiv_key = ("ARXIV", arxiv_id)
                    if arxiv_key not in seen:
                        seen.add(arxiv_key)
                        result.append(("ARXIV", arxiv_id, original))
            elif scheme == "PMID":
                normalized = "".join(character for character in original if character.isdigit())
                if not normalized:
                    raise HTTPException(status_code=422, detail="invalid PMID")
            elif scheme == "ARXIV":
                try:
                    normalized = normalize_arxiv(original)
                except ValueError as error:
                    raise HTTPException(
                        status_code=422, detail="invalid arXiv identifier"
                    ) from error
            elif scheme == "ISBN":
                normalized = "".join(
                    character
                    for character in original.upper()
                    if character.isdigit() or character == "X"
                )
            key = (scheme, normalized)
            if key not in seen:
                seen.add(key)
                result.append((scheme, normalized, original))
        return result

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text_value = str(value or "").strip()
        return text_value or None

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if value in {None, ""}:
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid publication_date") from error


paper_service = PaperService()
