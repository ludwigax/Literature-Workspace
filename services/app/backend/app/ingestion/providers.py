from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from html import unescape
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from backend.app.config import get_settings

MetadataSource = Literal["CROSSREF", "OPENALEX", "ARXIV"]

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ARXIV_ID_RE = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]+(?:\.[a-z]{2})?/\d{7})", re.I)


def _normalize_arxiv(value: str) -> str:
    clean = re.sub(r"v\d+$", "", value.strip(), flags=re.I)
    if _ARXIV_ID_RE.fullmatch(clean) is None:
        raise ValueError("invalid arXiv identifier")
    return clean.casefold()


def normalize_doi(value: str) -> str:
    clean = value.strip()
    lowered = clean.casefold()
    for prefix in (
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "https://doi.org/",
        "http://doi.org/",
        "doi:",
    ):
        if lowered.startswith(prefix):
            clean = clean[len(prefix) :].strip()
            break
    match = _DOI_RE.fullmatch(clean)
    if match is None:
        raise ValueError("invalid DOI")
    return clean.casefold()


def extract_dois(value: str) -> list[str]:
    result: list[str] = []
    for match in _DOI_RE.finditer(value):
        candidate = match.group(0).rstrip(".,;:)]}")
        try:
            normalized = normalize_doi(candidate)
        except ValueError:
            continue
        if normalized not in result:
            result.append(normalized)
    return result


@dataclass(frozen=True, slots=True)
class ResolvedMetadata:
    source: MetadataSource
    source_record_id: str
    doi: str
    title: str
    abstract: str | None
    publication_year: int | None
    publication_month: int | None
    publication_day: int | None
    publication_date: date | None
    publication_date_precision: str | None
    work_type: str | None
    venue: str | None
    canonical_url: str | None
    publisher: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    article_number: str | None
    language: str | None
    issn: list[str]
    isbn: list[str]
    authors: list[dict[str, Any]]
    extra: dict[str, Any]
    related_identifiers: tuple[tuple[str, str], ...] = ()


class MetadataResolver(Protocol):
    async def resolve(
        self, identifier: str, *, scheme: Literal["DOI", "ARXIV"] = "DOI"
    ) -> ResolvedMetadata | None: ...


class DoiMetadataResolver:
    """Resolve a DOI from one complete source, preferring Crossref."""

    def __init__(
        self,
        *,
        crossref_base_url: str,
        openalex_base_url: str,
        arxiv_api_base_url: str = "https://export.arxiv.org/api",
        arxiv_min_interval_seconds: float = 3.0,
        timeout_seconds: float,
        mailto: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.crossref_base_url = crossref_base_url.rstrip("/")
        self.openalex_base_url = openalex_base_url.rstrip("/")
        self.arxiv_api_base_url = arxiv_api_base_url.rstrip("/")
        self.arxiv_min_interval_seconds = arxiv_min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.mailto = mailto
        self._client = client
        self._arxiv_lock = asyncio.Lock()
        self._last_arxiv_request_at = 0.0

    async def resolve(
        self, identifier: str, *, scheme: Literal["DOI", "ARXIV"] = "DOI"
    ) -> ResolvedMetadata | None:
        if scheme == "ARXIV" or identifier.casefold().startswith("10.48550/arxiv."):
            arxiv_id = (
                _normalize_arxiv(identifier)
                if scheme == "ARXIV"
                else _normalize_arxiv(identifier[len("10.48550/arxiv.") :])
            )
            return await self._with_client(lambda client: self._resolve_arxiv(client, arxiv_id))
        normalized = normalize_doi(identifier)
        return await self._with_client(lambda client: self._resolve_with_client(client, normalized))

    async def _with_client(
        self,
        operation: Callable[[httpx.AsyncClient], Awaitable[ResolvedMetadata | None]],
    ) -> ResolvedMetadata | None:
        if self._client is not None:
            return await operation(self._client)
        headers = {"User-Agent": self._user_agent()}
        async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=headers) as client:
            return await operation(client)

    async def _resolve_with_client(
        self, client: httpx.AsyncClient, doi: str
    ) -> ResolvedMetadata | None:
        crossref = await self._crossref(client, doi)
        if crossref is not None:
            return crossref
        return await self._openalex(client, doi)

    async def _resolve_arxiv(
        self, client: httpx.AsyncClient, arxiv_id: str
    ) -> ResolvedMetadata | None:
        arxiv_doi = f"10.48550/arXiv.{arxiv_id}"
        response = await self._get_arxiv_response(client, arxiv_id)
        arxiv = (
            self._from_arxiv(response.text, arxiv_id)
            if response is not None and response.status_code == 200
            else None
        )
        if arxiv is None:
            fallback = await self._openalex(client, arxiv_doi)
            return self._with_arxiv_aliases(fallback, arxiv_id)
        formal_doi = str(arxiv.extra.get("published_doi") or "").strip()
        if formal_doi and not formal_doi.casefold().startswith("10.48550/arxiv."):
            published = await self._resolve_with_client(client, formal_doi)
            if published is not None:
                return self._with_arxiv_aliases(published, arxiv_id)
        return arxiv

    async def _get_arxiv_response(
        self, client: httpx.AsyncClient, arxiv_id: str
    ) -> httpx.Response | None:
        async with self._arxiv_lock:
            remaining = self.arxiv_min_interval_seconds - (
                time.monotonic() - self._last_arxiv_request_at
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                response = await client.get(
                    f"{self.arxiv_api_base_url}/query",
                    params={"id_list": arxiv_id, "max_results": 1},
                )
            except httpx.RequestError:
                self._last_arxiv_request_at = time.monotonic()
                return None
            self._last_arxiv_request_at = time.monotonic()
            if response.status_code != 429:
                return response

            retry_after = response.headers.get("Retry-After", "")
            try:
                retry_delay = float(retry_after)
            except ValueError:
                retry_delay = self.arxiv_min_interval_seconds
            await asyncio.sleep(min(max(retry_delay, self.arxiv_min_interval_seconds), 10.0))
            try:
                response = await client.get(
                    f"{self.arxiv_api_base_url}/query",
                    params={"id_list": arxiv_id, "max_results": 1},
                )
            except httpx.RequestError:
                return None
            finally:
                self._last_arxiv_request_at = time.monotonic()
            return response

    @staticmethod
    def _with_arxiv_aliases(
        value: ResolvedMetadata | None, arxiv_id: str
    ) -> ResolvedMetadata | None:
        if value is None:
            return None
        aliases = tuple(
            dict.fromkeys(
                (
                    *value.related_identifiers,
                    ("ARXIV", arxiv_id),
                    ("DOI", f"10.48550/arXiv.{arxiv_id}"),
                )
            )
        )
        return ResolvedMetadata(
            source=value.source,
            source_record_id=value.source_record_id,
            doi=value.doi,
            title=value.title,
            abstract=value.abstract,
            publication_year=value.publication_year,
            publication_month=value.publication_month,
            publication_day=value.publication_day,
            publication_date=value.publication_date,
            publication_date_precision=value.publication_date_precision,
            work_type=value.work_type,
            venue=value.venue,
            canonical_url=value.canonical_url,
            publisher=value.publisher,
            volume=value.volume,
            issue=value.issue,
            pages=value.pages,
            article_number=value.article_number,
            language=value.language,
            issn=value.issn,
            isbn=value.isbn,
            authors=value.authors,
            extra=value.extra,
            related_identifiers=aliases,
        )

    @staticmethod
    def _from_arxiv(xml_text: str, arxiv_id: str) -> ResolvedMetadata | None:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None
        atom = "{http://www.w3.org/2005/Atom}"
        arxiv = "{http://arxiv.org/schemas/atom}"
        entry = root.find(f"{atom}entry")
        if entry is None:
            return None
        title = " ".join((entry.findtext(f"{atom}title") or "").split())
        if not title:
            return None
        published = DoiMetadataResolver._parse_iso_date(
            (entry.findtext(f"{atom}published") or "")[:10]
        )
        authors = []
        for author in entry.findall(f"{atom}author"):
            name = " ".join((author.findtext(f"{atom}name") or "").split())
            if name:
                authors.append({"name": name})
        categories = [
            str(value.attrib.get("term"))
            for value in entry.findall(f"{atom}category")
            if value.attrib.get("term")
        ]
        formal_doi = (entry.findtext(f"{arxiv}doi") or "").strip() or None
        return ResolvedMetadata(
            source="ARXIV",
            source_record_id=f"https://arxiv.org/abs/{arxiv_id}",
            doi=f"10.48550/arXiv.{arxiv_id}",
            title=title,
            abstract=" ".join((entry.findtext(f"{atom}summary") or "").split()) or None,
            publication_year=published.year if published else None,
            publication_month=published.month if published else None,
            publication_day=published.day if published else None,
            publication_date=published,
            publication_date_precision="DAY" if published else None,
            work_type="PREPRINT",
            venue=(entry.findtext(f"{arxiv}journal_ref") or "").strip() or None,
            canonical_url=f"https://arxiv.org/abs/{arxiv_id}",
            publisher=None,
            volume=None,
            issue=None,
            pages=None,
            article_number=None,
            language=None,
            issn=[],
            isbn=[],
            authors=authors,
            extra={
                "arxiv_id": arxiv_id,
                "categories": categories,
                "comment": (entry.findtext(f"{arxiv}comment") or "").strip() or None,
                "published_doi": formal_doi,
            },
            related_identifiers=(("ARXIV", arxiv_id),),
        )

    async def _crossref(self, client: httpx.AsyncClient, doi: str) -> ResolvedMetadata | None:
        try:
            response = await client.get(
                f"{self.crossref_base_url}/works/{quote(doi, safe='')}",
                params={"mailto": self.mailto} if self.mailto else None,
            )
        except httpx.RequestError:
            return None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            return None
        payload = response.json().get("message")
        if not isinstance(payload, dict):
            return None
        return self._from_crossref(payload, doi)

    async def _openalex(self, client: httpx.AsyncClient, doi: str) -> ResolvedMetadata | None:
        external_id = quote(f"https://doi.org/{doi}", safe="")
        try:
            response = await client.get(
                f"{self.openalex_base_url}/works/{external_id}",
                params={"mailto": self.mailto} if self.mailto else None,
            )
        except httpx.RequestError:
            return None
        if response.status_code != 200:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        return self._from_openalex(payload, doi)

    @staticmethod
    def _from_crossref(payload: dict[str, Any], doi: str) -> ResolvedMetadata | None:
        title = DoiMetadataResolver._first_text(payload.get("title"))
        if title is None:
            return None
        published = payload.get("published") or payload.get("published-print") or {}
        year, month, day, publication_date, precision = DoiMetadataResolver._date_parts(
            published.get("date-parts")
        )
        authors: list[dict[str, Any]] = []
        for value in payload.get("author") or []:
            if not isinstance(value, dict):
                continue
            given = str(value.get("given") or "").strip()
            family = str(value.get("family") or "").strip()
            name = " ".join(part for part in (given, family) if part)
            if name:
                authors.append(
                    {
                        "name": name,
                        "given": given or None,
                        "family": family or None,
                        "orcid": value.get("ORCID"),
                    }
                )
        abstract = DoiMetadataResolver._plain_text(payload.get("abstract"))
        return ResolvedMetadata(
            source="CROSSREF",
            source_record_id=str(payload.get("URL") or payload.get("DOI") or doi),
            doi=doi,
            title=title,
            abstract=abstract,
            publication_year=year,
            publication_month=month,
            publication_day=day,
            publication_date=publication_date,
            publication_date_precision=precision,
            work_type=DoiMetadataResolver._normalize_work_type(payload.get("type")),
            venue=DoiMetadataResolver._first_text(payload.get("container-title")),
            canonical_url=str(payload.get("URL") or "").strip() or None,
            publisher=DoiMetadataResolver._first_text(payload.get("publisher")),
            volume=DoiMetadataResolver._first_text(payload.get("volume")),
            issue=DoiMetadataResolver._first_text(payload.get("issue")),
            pages=DoiMetadataResolver._first_text(payload.get("page")),
            article_number=DoiMetadataResolver._first_text(payload.get("article-number")),
            language=DoiMetadataResolver._first_text(payload.get("language")),
            issn=DoiMetadataResolver._text_list(payload.get("ISSN")),
            isbn=DoiMetadataResolver._text_list(payload.get("ISBN")),
            authors=authors,
            extra={
                "source_type": payload.get("type"),
            },
        )

    @staticmethod
    def _from_openalex(payload: dict[str, Any], doi: str) -> ResolvedMetadata | None:
        # This preserves the field mapping used by the repository's existing
        # OpenAlexWorkData adapter while using the v2 async HTTP transport.
        title = str(payload.get("title") or "").strip()
        if not title:
            return None
        authors: list[dict[str, Any]] = []
        for authorship in payload.get("authorships") or []:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author") or {}
            name = str(author.get("display_name") or "").strip()
            if name:
                authors.append(
                    {
                        "name": name,
                        "openalex_id": author.get("id"),
                        "orcid": author.get("orcid"),
                        "is_corresponding": bool(authorship.get("is_corresponding")),
                    }
                )
        primary_location = payload.get("primary_location") or {}
        source = primary_location.get("source") or {}
        publication_date = DoiMetadataResolver._parse_iso_date(payload.get("publication_date"))
        biblio = payload.get("biblio") or {}
        first_page = str(biblio.get("first_page") or "").strip()
        last_page = str(biblio.get("last_page") or "").strip()
        pages = "-".join(value for value in (first_page, last_page) if value) or None
        landing_url = str(primary_location.get("landing_page_url") or "").strip() or None
        return ResolvedMetadata(
            source="OPENALEX",
            source_record_id=str(payload.get("id") or doi),
            doi=doi,
            title=title,
            abstract=DoiMetadataResolver._reconstruct_abstract(
                payload.get("abstract_inverted_index")
            ),
            publication_year=payload.get("publication_year"),
            publication_month=publication_date.month if publication_date else None,
            publication_day=publication_date.day if publication_date else None,
            publication_date=publication_date,
            publication_date_precision="DAY"
            if publication_date
            else ("YEAR" if payload.get("publication_year") else None),
            work_type=DoiMetadataResolver._normalize_work_type(
                payload.get("type_crossref") or payload.get("type")
            ),
            venue=str(source.get("display_name") or "").strip() or None,
            canonical_url=landing_url,
            publisher=str(source.get("host_organization_name") or "").strip() or None,
            volume=str(biblio.get("volume") or "").strip() or None,
            issue=str(biblio.get("issue") or "").strip() or None,
            pages=pages,
            article_number=str(biblio.get("article_number") or "").strip() or None,
            language=str(payload.get("language") or "").strip() or None,
            issn=DoiMetadataResolver._text_list(source.get("issn")),
            isbn=[],
            authors=authors,
            extra={
                "source_type": payload.get("type"),
                "cited_by_count": payload.get("cited_by_count", 0),
                "is_open_access": bool((payload.get("open_access") or {}).get("is_oa")),
            },
        )

    def _user_agent(self) -> str:
        suffix = f" (mailto:{self.mailto})" if self.mailto else ""
        return f"LiteratureWorkspaceV2/0.1{suffix}"

    @staticmethod
    def _first_text(value: Any) -> str | None:
        if isinstance(value, list) and value:
            value = value[0]
        clean = str(value or "").strip()
        return clean or None

    @staticmethod
    def _plain_text(value: Any) -> str | None:
        clean = _TAG_RE.sub(" ", unescape(str(value or "")))
        clean = " ".join(clean.split())
        return clean or None

    @staticmethod
    def _date_from_parts(value: Any) -> date | None:
        return DoiMetadataResolver._date_parts(value)[3]

    @staticmethod
    def _date_parts(
        value: Any,
    ) -> tuple[int | None, int | None, int | None, date | None, str | None]:
        if not isinstance(value, list) or not value or not isinstance(value[0], list):
            return None, None, None, None, None
        parts = value[0]
        try:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else None
            day = int(parts[2]) if len(parts) > 2 else None
            precision = "DAY" if day is not None else "MONTH" if month is not None else "YEAR"
            exact = date(year, month, day) if month is not None and day is not None else None
            return year, month, day, exact, precision
        except (IndexError, TypeError, ValueError):
            return None, None, None, None, None

    @staticmethod
    def _parse_iso_date(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value)) if value else None
        except ValueError:
            return None

    @staticmethod
    def _text_list(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in values if str(item or "").strip()]

    @staticmethod
    def _normalize_work_type(value: Any) -> str | None:
        normalized = str(value or "").strip().casefold().replace("_", "-")
        mapping = {
            "journal-article": "JOURNAL_ARTICLE",
            "article": "JOURNAL_ARTICLE",
            "review": "REVIEW",
            "posted-content": "PREPRINT",
            "preprint": "PREPRINT",
            "book-chapter": "BOOK_CHAPTER",
            "book-section": "BOOK_CHAPTER",
            "book": "BOOK",
            "monograph": "BOOK",
            "proceedings-article": "CONFERENCE_PAPER",
            "proceedings": "CONFERENCE_PAPER",
            "dissertation": "THESIS",
            "report": "REPORT",
            "dataset": "DATASET",
        }
        return mapping.get(normalized, "OTHER" if normalized else None)

    @staticmethod
    def _reconstruct_abstract(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        positions: list[tuple[int, str]] = []
        for word, indexes in value.items():
            if not isinstance(indexes, list):
                continue
            positions.extend((index, str(word)) for index in indexes if isinstance(index, int))
        positions.sort()
        return " ".join(word for _, word in positions) or None


@lru_cache
def get_metadata_resolver() -> DoiMetadataResolver:
    settings = get_settings()
    return DoiMetadataResolver(
        crossref_base_url=settings.crossref_base_url,
        openalex_base_url=settings.openalex_base_url,
        arxiv_api_base_url=settings.arxiv_api_base_url,
        arxiv_min_interval_seconds=settings.arxiv_min_interval_seconds,
        timeout_seconds=settings.metadata_provider_timeout_seconds,
        mailto=settings.scholarly_api_mailto,
    )
