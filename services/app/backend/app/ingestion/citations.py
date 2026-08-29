from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .providers import extract_dois


@dataclass(frozen=True, slots=True)
class LimitedCitation:
    title: str
    abstract: str | None
    publication_year: int | None
    venue: str | None
    authors: list[dict[str, Any]]
    doi: str | None
    import_key: str | None

    def metadata(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "abstract": self.abstract,
            "publication_year": self.publication_year,
            "venue": self.venue,
            "authors": self.authors,
            "extra": {"import_key": self.import_key} if self.import_key else {},
            "provenance": {"source": "citation_file"},
        }


def parse_citation_file(data: bytes, filename: str) -> list[LimitedCitation]:
    text = data.decode("utf-8-sig", errors="replace")
    lowered = filename.casefold()
    if lowered.endswith(".bib") or "@article" in text.casefold() or "@book" in text.casefold():
        values = _parse_bibtex(text)
    elif lowered.endswith(".ris") or re.search(r"(?m)^TY  - ", text):
        values = _parse_ris(text)
    elif lowered.endswith(".json"):
        values = _parse_csl_json(text)
    else:
        raise ValueError("Supported citation formats are BibTeX, RIS, and CSL-JSON")
    if not values:
        raise ValueError("Citation file contains no usable records")
    return values


def _parse_bibtex(text: str) -> list[LimitedCitation]:
    records: list[LimitedCitation] = []
    for entry_type, key, body in _bib_entries(text):
        fields = _bib_fields(body)
        title = _clean_bib(fields.get("title")) or key or f"Untitled {entry_type}"
        author_text = _clean_bib(fields.get("author"))
        authors = [
            {"name": value.strip()}
            for value in re.split(r"\s+and\s+", author_text or "")
            if value.strip()
        ]
        doi = _first_doi(fields.get("doi") or " ".join(fields.values()))
        records.append(
            LimitedCitation(
                title=title,
                abstract=_clean_bib(fields.get("abstract")),
                publication_year=_year(fields.get("year")),
                venue=_clean_bib(fields.get("journal") or fields.get("booktitle")),
                authors=authors,
                doi=doi,
                import_key=key or None,
            )
        )
    return records


def _bib_entries(text: str) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    cursor = 0
    while True:
        match = re.search(r"@(\w+)\s*([({])", text[cursor:], re.IGNORECASE)
        if match is None:
            break
        start = cursor + match.end()
        opening = match.group(2)
        closing = "}" if opening == "{" else ")"
        depth = 1
        quote_open = False
        index = start
        while index < len(text) and depth:
            char = text[index]
            if char == '"' and (index == 0 or text[index - 1] != "\\"):
                quote_open = not quote_open
            elif not quote_open:
                if char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
            index += 1
        content = text[start : index - 1]
        key, _, body = content.partition(",")
        if body:
            result.append((match.group(1).casefold(), key.strip(), body))
        cursor = max(index, start)
    return result


def _bib_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    cursor = 0
    while cursor < len(body):
        match = re.search(r"([A-Za-z][\w-]*)\s*=\s*", body[cursor:])
        if match is None:
            break
        name = match.group(1).casefold()
        start = cursor + match.end()
        if start >= len(body):
            break
        opener = body[start]
        if opener in {"{", '"'}:
            closer = "}" if opener == "{" else '"'
            depth = 1
            index = start + 1
            while index < len(body) and depth:
                char = body[index]
                if opener == "{" and char == "{":
                    depth += 1
                elif char == closer and body[index - 1] != "\\":
                    depth -= 1
                index += 1
            value = body[start + 1 : index - 1]
        else:
            index = body.find(",", start)
            index = len(body) if index < 0 else index
            value = body[start:index]
        fields[name] = value.strip()
        cursor = index + 1
    return fields


def _parse_ris(text: str) -> list[LimitedCitation]:
    records: list[LimitedCitation] = []
    current: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z0-9]{2})  - (.*)$", line)
        if match is None:
            continue
        tag, value = match.groups()
        if tag == "TY":
            current = {"TY": [value]}
        elif tag == "ER":
            if current:
                records.append(_ris_record(current))
            current = {}
        else:
            current.setdefault(tag, []).append(value.strip())
    if current:
        records.append(_ris_record(current))
    return records


def _ris_record(fields: dict[str, list[str]]) -> LimitedCitation:
    def first(*tags: str) -> str | None:
        for tag in tags:
            if fields.get(tag):
                return fields[tag][0].strip() or None
        return None

    combined = " ".join(value for values in fields.values() for value in values)
    return LimitedCitation(
        title=first("TI", "T1", "CT") or "Untitled imported record",
        abstract=first("AB", "N2"),
        publication_year=_year(first("PY", "Y1", "DA")),
        venue=first("JO", "JF", "T2"),
        authors=[{"name": value} for value in fields.get("AU", fields.get("A1", []))],
        doi=_first_doi(first("DO") or combined),
        import_key=first("ID"),
    )


def _parse_csl_json(text: str) -> list[LimitedCitation]:
    raw = json.loads(text)
    values = raw if isinstance(raw, list) else [raw]
    records: list[LimitedCitation] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        authors: list[dict[str, Any]] = []
        for author in value.get("author") or []:
            if not isinstance(author, dict):
                continue
            name = str(author.get("literal") or "").strip()
            if not name:
                parts = (
                    str(author.get("given") or "").strip(),
                    str(author.get("family") or "").strip(),
                )
                name = " ".join(part for part in parts if part)
            if name:
                authors.append(
                    {
                        "name": name,
                        "given": author.get("given"),
                        "family": author.get("family"),
                    }
                )
        issued = value.get("issued") or {}
        date_parts = issued.get("date-parts") or []
        year = _year(date_parts[0][0]) if date_parts and date_parts[0] else None
        combined = json.dumps(value, ensure_ascii=False)
        records.append(
            LimitedCitation(
                title=str(value.get("title") or "Untitled imported record").strip(),
                abstract=str(value.get("abstract") or "").strip() or None,
                publication_year=year,
                venue=str(value.get("container-title") or "").strip() or None,
                authors=authors,
                doi=_first_doi(str(value.get("DOI") or "") or combined),
                import_key=str(value.get("id") or "").strip() or None,
            )
        )
    return records


def _clean_bib(value: str | None) -> str | None:
    if value is None:
        return None
    clean = re.sub(r"[{}]", "", value)
    clean = re.sub(r"\\[A-Za-z]+", "", clean)
    clean = " ".join(clean.split())
    return clean or None


def _first_doi(value: str) -> str | None:
    values = extract_dois(value)
    return values[0] if values else None


def _year(value: Any) -> int | None:
    match = re.search(r"(?:^|\D)((?:1[5-9]|20|21)\d{2})(?:\D|$)", str(value or ""))
    return int(match.group(1)) if match else None
