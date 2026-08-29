from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .identifiers import extract_arxiv_ids
from .providers import extract_dois

ZOTERO_IMPORT_JOB = "ZOTERO_IMPORT"
_NON_BIBLIOGRAPHIC_TYPES = {"attachment", "note", "annotation"}


def _zotero_work_type(value: str) -> str | None:
    mapping = {
        "journalarticle": "JOURNAL_ARTICLE",
        "preprint": "PREPRINT",
        "booksection": "BOOK_CHAPTER",
        "book": "BOOK",
        "conferencepaper": "CONFERENCE_PAPER",
        "thesis": "THESIS",
        "report": "REPORT",
    }
    normalized = value.strip().casefold()
    return mapping.get(normalized, "OTHER" if normalized else None)


def _zotero_date_parts(
    value: str | None,
) -> tuple[int | None, int | None, int | None, date | None, str | None]:
    text = str(value or "").strip()
    year_match = re.search(r"(?:^|\D)((?:1[5-9]|20|21)\d{2})(?:\D|$)", text)
    if year_match is None:
        return None, None, None, None, None
    year = int(year_match.group(1))
    exact_match = re.search(rf"{year}[-/.](\d{{1,2}})(?:[-/.](\d{{1,2}}))?", text)
    if exact_match is None:
        return year, None, None, None, "YEAR"
    month = int(exact_match.group(1))
    day = int(exact_match.group(2)) if exact_match.group(2) else None
    try:
        exact = date(year, month, day) if day is not None else None
    except ValueError:
        return year, None, None, None, "YEAR"
    return year, month, day, exact, "DAY" if day is not None else "MONTH"


def _identifier_values(value: str | None) -> list[str]:
    return [part.strip() for part in re.split(r"[,;]", str(value or "")) if part.strip()]


@dataclass(frozen=True, slots=True)
class ZoteroCollectionRecord:
    zotero_library_id: int
    key: str
    name: str
    parent_key: str | None


@dataclass(frozen=True, slots=True)
class ZoteroItemRecord:
    zotero_library_id: int
    key: str
    version: int
    item_type: str
    metadata: dict[str, Any]
    identifiers: tuple[dict[str, str], ...]
    collection_keys: tuple[str, ...]
    attachments: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ZoteroSnapshot:
    source_identity: str
    display_name: str | None
    schema_version: int
    collections: tuple[ZoteroCollectionRecord, ...]
    items: tuple[ZoteroItemRecord, ...]


def parse_zotero_snapshot(data: bytes) -> ZoteroSnapshot:
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("Uploaded file is not a SQLite database")
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as temporary:
            temporary.write(data)
            path = temporary.name
        connection = sqlite3.connect(
            f"file:{Path(path).as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            return _parse_connection(connection)
        finally:
            connection.close()
    finally:
        if path is not None:
            os.unlink(path)


def _parse_connection(connection: sqlite3.Connection) -> ZoteroSnapshot:
    required = {
        "items",
        "itemTypes",
        "itemData",
        "itemDataValues",
        "fields",
        "creators",
        "itemCreators",
        "creatorTypes",
        "collections",
        "collectionItems",
        "itemAttachments",
    }
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if missing := required - tables:
        raise ValueError(
            f"Unsupported Zotero database; missing tables: {', '.join(sorted(missing))}"
        )
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Zotero database failed SQLite integrity checking")

    schema_row = connection.execute(
        "SELECT version FROM version WHERE schema='userdata'"
    ).fetchone()
    schema_version = int(schema_row[0]) if schema_row else 0
    user_id = _setting(connection, "account", "userID")
    username = _setting(connection, "account", "username")
    if user_id is None:
        user = connection.execute("SELECT userID, name FROM users LIMIT 1").fetchone()
        user_id = user[0] if user else "local"
        username = username or (user[1] if user else None)
    source_identity = f"zotero-user:{user_id}"

    collection_rows = list(
        connection.execute(
            """
            SELECT c.libraryID, c.key, c.collectionName, parent.key AS parentKey
            FROM collections c
            LEFT JOIN collections parent ON parent.collectionID = c.parentCollectionID
            ORDER BY c.parentCollectionID IS NOT NULL, c.collectionID
            """
        )
    )
    collections = tuple(
        ZoteroCollectionRecord(
            zotero_library_id=int(row["libraryID"]),
            key=str(row["key"]),
            name=str(row["collectionName"]),
            parent_key=str(row["parentKey"]) if row["parentKey"] else None,
        )
        for row in collection_rows
    )

    item_rows = list(
        connection.execute(
            """
            SELECT i.itemID, i.libraryID, i.key, i.version, t.typeName
            FROM items i
            JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
            LEFT JOIN deletedItems d ON d.itemID = i.itemID
            WHERE d.itemID IS NULL
            ORDER BY i.itemID
            """
        )
    )
    bibliographic = {
        int(row["itemID"]): row
        for row in item_rows
        if str(row["typeName"]) not in _NON_BIBLIOGRAPHIC_TYPES
    }
    fields: dict[int, dict[str, str]] = {item_id: {} for item_id in bibliographic}
    if bibliographic:
        placeholders = ",".join("?" for _ in bibliographic)
        for row in connection.execute(
            f"""
            SELECT d.itemID, f.fieldName, v.value
            FROM itemData d
            JOIN fields f ON f.fieldID = d.fieldID
            JOIN itemDataValues v ON v.valueID = d.valueID
            WHERE d.itemID IN ({placeholders})
            """,
            tuple(bibliographic),
        ):
            fields[int(row["itemID"])][str(row["fieldName"])] = str(row["value"])

    creators: dict[int, list[dict[str, Any]]] = {item_id: [] for item_id in bibliographic}
    for row in connection.execute(
        """
        SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode,
               ct.creatorType, ic.orderIndex
        FROM itemCreators ic
        JOIN creators c ON c.creatorID = ic.creatorID
        JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
        ORDER BY ic.itemID, ic.orderIndex
        """
    ):
        item_id = int(row["itemID"])
        if item_id not in creators:
            continue
        given = str(row["firstName"] or "").strip()
        family = str(row["lastName"] or "").strip()
        name = (
            family
            if int(row["fieldMode"] or 0)
            else " ".join(value for value in (given, family) if value)
        )
        if name:
            creators[item_id].append(
                {
                    "name": name,
                    "given": given or None,
                    "family": family or None,
                    "role": str(row["creatorType"]),
                }
            )

    placements: dict[int, list[str]] = {item_id: [] for item_id in bibliographic}
    for row in connection.execute(
        """
        SELECT ci.itemID, c.key
        FROM collectionItems ci
        JOIN collections c ON c.collectionID = ci.collectionID
        ORDER BY ci.itemID, ci.orderIndex
        """
    ):
        item_id = int(row["itemID"])
        if item_id in placements:
            placements[item_id].append(str(row["key"]))

    attachments: dict[int, list[dict[str, Any]]] = {item_id: [] for item_id in bibliographic}
    for row in connection.execute(
        """
        SELECT a.parentItemID, i.key, i.version, a.path, a.contentType,
               a.linkMode, a.storageHash
        FROM itemAttachments a
        JOIN items i ON i.itemID = a.itemID
        WHERE a.parentItemID IS NOT NULL
        ORDER BY a.parentItemID, i.itemID
        """
    ):
        parent_id = int(row["parentItemID"])
        if parent_id in attachments:
            attachments[parent_id].append(
                {
                    "item_key": str(row["key"]),
                    "version": int(row["version"]),
                    "path": str(row["path"] or ""),
                    "content_type": str(row["contentType"] or ""),
                    "link_mode": int(row["linkMode"]),
                    "storage_hash": str(row["storageHash"] or "") or None,
                    "file_available": False,
                }
            )

    records: list[ZoteroItemRecord] = []
    for item_id, row in bibliographic.items():
        item_fields = fields[item_id]
        title = item_fields.get("title", "").strip() or f"Untitled Zotero item {row['key']}"
        identifiers = _identifiers(item_fields)
        year, month, day, publication_date, precision = _zotero_date_parts(item_fields.get("date"))
        known = {
            "title",
            "abstractNote",
            "date",
            "publicationTitle",
            "proceedingsTitle",
            "bookTitle",
            "repository",
            "DOI",
            "archiveID",
            "url",
            "publisher",
            "volume",
            "issue",
            "pages",
            "articleNumber",
            "language",
            "ISSN",
            "ISBN",
        }
        records.append(
            ZoteroItemRecord(
                zotero_library_id=int(row["libraryID"]),
                key=str(row["key"]),
                version=int(row["version"]),
                item_type=str(row["typeName"]),
                metadata={
                    "title": title,
                    "abstract": item_fields.get("abstractNote") or None,
                    "publication_year": year,
                    "publication_month": month,
                    "publication_day": day,
                    "publication_date": publication_date.isoformat() if publication_date else None,
                    "publication_date_precision": precision,
                    "work_type": _zotero_work_type(str(row["typeName"])),
                    "venue": next(
                        (
                            item_fields.get(key)
                            for key in (
                                "publicationTitle",
                                "proceedingsTitle",
                                "bookTitle",
                                "repository",
                            )
                            if item_fields.get(key)
                        ),
                        None,
                    ),
                    "canonical_url": item_fields.get("url") or None,
                    "publisher": item_fields.get("publisher") or None,
                    "volume": item_fields.get("volume") or None,
                    "issue": item_fields.get("issue") or None,
                    "pages": item_fields.get("pages") or None,
                    "article_number": item_fields.get("articleNumber") or None,
                    "language": item_fields.get("language") or None,
                    "issn": _identifier_values(item_fields.get("ISSN")),
                    "isbn": _identifier_values(item_fields.get("ISBN")),
                    "authors": creators[item_id],
                    "extra": {
                        "zotero_item_type": str(row["typeName"]),
                        "zotero_fields": {
                            key: value for key, value in item_fields.items() if key not in known
                        },
                    },
                    "provenance": {
                        "source": "zotero",
                        "zotero_library_id": int(row["libraryID"]),
                        "zotero_item_key": str(row["key"]),
                        "zotero_item_version": int(row["version"]),
                    },
                },
                identifiers=identifiers,
                collection_keys=tuple(placements[item_id]),
                attachments=tuple(attachments[item_id]),
            )
        )
    return ZoteroSnapshot(
        source_identity=source_identity,
        display_name=str(username) if username else None,
        schema_version=schema_version,
        collections=collections,
        items=tuple(records),
    )


def _setting(connection: sqlite3.Connection, setting: str, key: str) -> Any:
    row = connection.execute(
        "SELECT value FROM settings WHERE setting=? AND key=?",
        (setting, key),
    ).fetchone()
    return row[0] if row else None


def _identifiers(fields: dict[str, str]) -> tuple[dict[str, str], ...]:
    combined = "\n".join(fields.get(key, "") for key in ("DOI", "archiveID", "extra", "url"))
    dois = extract_dois(combined)
    arxiv_ids = extract_arxiv_ids(combined)
    if arxiv_ids and not dois:
        dois.append(f"10.48550/arXiv.{arxiv_ids[0]}")
    result = [{"scheme": "DOI", "value": value} for value in dois[:2]]
    result.extend({"scheme": "ARXIV", "value": value} for value in arxiv_ids[:1])
    return tuple(result)


def _year(value: str | None) -> int | None:
    match = re.search(r"(?:^|\D)((?:1[5-9]|20|21)\d{2})(?:\D|$)", value or "")
    return int(match.group(1)) if match else None
