from __future__ import annotations

import re
from dataclasses import dataclass

from .providers import extract_dois, normalize_doi

_ARXIV_MODERN_RE = re.compile(r"(?<![\w.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\w.])", re.I)
_ARXIV_LEGACY_RE = re.compile(
    r"(?<![\w./])([a-z][a-z0-9.-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?![\w/])",
    re.I,
)
_ARXIV_DOI_PREFIX = "10.48550/arxiv."


@dataclass(frozen=True, slots=True)
class ScholarlyIdentifier:
    scheme: str
    normalized_value: str
    original_value: str
    evidence: str


@dataclass(frozen=True, slots=True)
class PdfIdentifierSelection:
    identifiers: tuple[ScholarlyIdentifier, ...]
    metadata_doi: str | None
    evidence_source: str | None


def normalize_arxiv(value: str) -> str:
    clean = value.strip()
    lowered = clean.casefold()
    for prefix in (
        "https://arxiv.org/abs/",
        "http://arxiv.org/abs/",
        "https://arxiv.org/pdf/",
        "http://arxiv.org/pdf/",
        "arxiv:",
    ):
        if lowered.startswith(prefix):
            clean = clean[len(prefix) :].strip()
            break
    clean = re.sub(r"\.pdf$", "", clean, flags=re.I)
    clean = re.sub(r"v\d+$", "", clean, flags=re.I)
    if not (_ARXIV_MODERN_RE.fullmatch(clean) or _ARXIV_LEGACY_RE.fullmatch(clean)):
        raise ValueError("invalid arXiv identifier")
    return clean.casefold()


def arxiv_id_from_datacite_doi(value: str) -> str | None:
    """Return the arXiv id represented by a 10.48550/arXiv DOI, if any."""

    normalized = normalize_doi(value)
    if not normalized.startswith(_ARXIV_DOI_PREFIX):
        return None
    try:
        return normalize_arxiv(normalized[len(_ARXIV_DOI_PREFIX) :])
    except ValueError:
        return None


def extract_arxiv_ids(value: str) -> list[str]:
    result: list[str] = []
    for doi in extract_dois(value):
        if doi.startswith(_ARXIV_DOI_PREFIX):
            candidate = doi[len(_ARXIV_DOI_PREFIX) :]
            try:
                normalized = normalize_arxiv(candidate)
            except ValueError:
                continue
            if normalized not in result:
                result.append(normalized)
    arxiv_text = re.sub(r"\.pdf\b", "", value, flags=re.I)
    for pattern in (_ARXIV_MODERN_RE, _ARXIV_LEGACY_RE):
        for match in pattern.finditer(arxiv_text):
            normalized = normalize_arxiv(match.group(1))
            if normalized not in result:
                result.append(normalized)
    return result


def select_pdf_identifiers(
    metadata_text: str, page_text: str, filename: str = ""
) -> PdfIdentifierSelection:
    """Select identifiers from PDF evidence, preferring the metadata dictionary.

    We intentionally use one evidence source at a time. This avoids treating a DOI
    from a title-page bibliography as belonging to the uploaded paper when the PDF
    metadata already provides an identifier.
    """

    if _has_identifiers(metadata_text):
        source, text = "PDF_METADATA", metadata_text
    elif _has_identifiers(page_text):
        source, text = "PDF_FIRST_PAGES", page_text
    else:
        source, text = "FILENAME", filename
    dois = extract_dois(text)
    arxiv_ids = extract_arxiv_ids(text)

    # A 10.48550/arXiv DOI and its arXiv id are aliases for the same deposit.
    for doi in dois:
        if doi.startswith(_ARXIV_DOI_PREFIX):
            arxiv_id = normalize_arxiv(doi[len(_ARXIV_DOI_PREFIX) :])
            if arxiv_id not in arxiv_ids:
                arxiv_ids.append(arxiv_id)

    selected_dois = _select_dois(dois)
    if not selected_dois and arxiv_ids:
        selected_dois.append(f"{_ARXIV_DOI_PREFIX}{arxiv_ids[0]}")
    values: list[ScholarlyIdentifier] = []
    for doi in selected_dois:
        values.append(ScholarlyIdentifier("DOI", normalize_doi(doi), doi, source))
    for arxiv_id in arxiv_ids[:1]:
        values.append(ScholarlyIdentifier("ARXIV", normalize_arxiv(arxiv_id), arxiv_id, source))

    # Prefer a publication DOI for metadata lookup; an arXiv DataCite DOI is still
    # usable by OpenAlex when it is the only DOI available.
    metadata_doi = next(
        (value for value in selected_dois if not value.startswith(_ARXIV_DOI_PREFIX)),
        selected_dois[0] if selected_dois else None,
    )
    return PdfIdentifierSelection(
        identifiers=tuple(values),
        metadata_doi=metadata_doi,
        evidence_source=source if values else None,
    )


def _has_identifiers(value: str) -> bool:
    return bool(extract_dois(value) or extract_arxiv_ids(value))


def _select_dois(values: list[str]) -> list[str]:
    publication = next((value for value in values if not value.startswith(_ARXIV_DOI_PREFIX)), None)
    arxiv = next((value for value in values if value.startswith(_ARXIV_DOI_PREFIX)), None)
    return [value for value in (publication, arxiv) if value is not None]
