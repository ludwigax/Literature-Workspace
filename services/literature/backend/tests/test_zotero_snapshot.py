import sqlite3

from backend.app.ingestion.zotero_import import merge_attachment_manifest
from backend.app.ingestion.zotero_snapshot import parse_zotero_snapshot


def test_parse_zotero_snapshot_preserves_metadata_collections_and_attachment_manifest() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE version (schema TEXT PRIMARY KEY, version INTEGER);
        INSERT INTO version VALUES ('userdata', 123);
        CREATE TABLE settings (setting TEXT, key TEXT, value, PRIMARY KEY (setting, key));
        INSERT INTO settings VALUES ('account', 'userID', 42);
        INSERT INTO settings VALUES ('account', 'username', 'alice-zotero');
        CREATE TABLE users (userID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        INSERT INTO itemTypes VALUES (1, 'preprint'), (2, 'attachment');
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, libraryID INTEGER,
            key TEXT, version INTEGER
        );
        INSERT INTO items VALUES (1, 1, 1, 'PAPERKEY', 7), (2, 2, 1, 'PDFKEY', 3);
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        INSERT INTO fields VALUES (1, 'title'), (2, 'archiveID'), (3, 'date');
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        INSERT INTO itemDataValues VALUES
            (1, 'A Zotero Paper'), (2, 'arXiv:2401.12345v2'), (3, '2024-01-02');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        INSERT INTO itemData VALUES (1, 1, 1), (1, 2, 2), (1, 3, 3);
        CREATE TABLE creators (
            creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INTEGER
        );
        INSERT INTO creators VALUES (1, 'Ada', 'Lovelace', 0);
        CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
        INSERT INTO creatorTypes VALUES (1, 'author');
        CREATE TABLE itemCreators (
            itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER
        );
        INSERT INTO itemCreators VALUES (1, 1, 1, 0);
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY, collectionName TEXT,
            parentCollectionID INTEGER, libraryID INTEGER, key TEXT
        );
        INSERT INTO collections VALUES (1, 'Research', NULL, 1, 'COLLKEY');
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER);
        INSERT INTO collectionItems VALUES (1, 1, 0);
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INTEGER, linkMode INTEGER,
            contentType TEXT, path TEXT, storageHash TEXT
        );
        INSERT INTO itemAttachments VALUES
            (2, 1, 0, 'application/pdf', 'storage:paper.pdf', 'abc123');
        """
    )
    data = connection.serialize()
    connection.close()

    snapshot = parse_zotero_snapshot(data)

    assert snapshot.source_identity == "zotero-user:42"
    assert snapshot.display_name == "alice-zotero"
    assert snapshot.schema_version == 123
    assert snapshot.collections[0].key == "COLLKEY"
    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.metadata["title"] == "A Zotero Paper"
    assert item.metadata["publication_year"] == 2024
    assert item.metadata["publication_month"] == 1
    assert item.metadata["publication_day"] == 2
    assert item.metadata["publication_date"] == "2024-01-02"
    assert item.metadata["publication_date_precision"] == "DAY"
    assert item.metadata["work_type"] == "PREPRINT"
    assert item.metadata["authors"] == [
        {
            "name": "Ada Lovelace",
            "given": "Ada",
            "family": "Lovelace",
            "role": "author",
        }
    ]
    assert item.identifiers == (
        {"scheme": "DOI", "value": "10.48550/arXiv.2401.12345"},
        {"scheme": "ARXIV", "value": "2401.12345"},
    )
    assert item.collection_keys == ("COLLKEY",)
    assert item.attachments[0]["path"] == "storage:paper.pdf"
    assert item.attachments[0]["file_available"] is False


def test_zotero_manifest_reuses_only_unchanged_uploaded_attachments() -> None:
    existing = [
        {
            "item_key": "UNCHANGED",
            "version": 2,
            "path": "storage:paper.pdf",
            "content_type": "application/pdf",
            "link_mode": 0,
            "storage_hash": "ABC123",
            "file_available": True,
            "blob_id": "blob-one",
            "import_role": "PRIMARY_PDF",
        },
        {
            "item_key": "CHANGED",
            "version": 1,
            "path": "storage:old.pdf",
            "content_type": "application/pdf",
            "link_mode": 0,
            "storage_hash": "OLD",
            "file_available": True,
            "blob_id": "blob-old",
            "import_role": "ASSET",
        },
    ]
    incoming = (
        {
            "item_key": "UNCHANGED",
            "version": 3,
            "path": "storage:paper.pdf",
            "content_type": "application/pdf",
            "link_mode": 0,
            "storage_hash": "abc123",
            "file_available": False,
        },
        {
            "item_key": "CHANGED",
            "version": 2,
            "path": "storage:new.pdf",
            "content_type": "application/pdf",
            "link_mode": 0,
            "storage_hash": "NEW",
            "file_available": False,
        },
        {
            "item_key": "NEW",
            "version": 1,
            "path": "storage:new-item.pdf",
            "content_type": "application/pdf",
            "link_mode": 0,
            "storage_hash": None,
            "file_available": False,
        },
    )

    merged = merge_attachment_manifest(incoming, existing)

    assert merged[0]["file_available"] is True
    assert merged[0]["blob_id"] == "blob-one"
    assert merged[0]["import_role"] == "PRIMARY_PDF"
    assert merged[1]["file_available"] is False
    assert "blob_id" not in merged[1]
    assert merged[2]["file_available"] is False
