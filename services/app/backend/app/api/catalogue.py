from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from ..authorization.dependencies import CsrfProtected, CurrentActor, Database
from ..catalogue.service import catalogue_service
from ..collections.service import collection_service

router = APIRouter(prefix="/libraries", tags=["catalogue"])


class IdentifierBody(BaseModel):
    scheme: Literal["DOI", "PMID", "ARXIV", "ISBN", "OTHER"]
    value: str = Field(min_length=1, max_length=500)


class MetadataBody(BaseModel):
    title: str = Field(min_length=1, max_length=10_000)
    abstract: str | None = Field(default=None, max_length=200_000)
    publication_year: int | None = Field(default=None, ge=1000, le=3000)
    publication_month: int | None = Field(default=None, ge=1, le=12)
    publication_day: int | None = Field(default=None, ge=1, le=31)
    publication_date: str | None = None
    publication_date_precision: Literal["YEAR", "MONTH", "DAY"] | None = None
    work_type: str | None = Field(default=None, max_length=50)
    venue: str | None = Field(default=None, max_length=10_000)
    canonical_url: str | None = Field(default=None, max_length=10_000)
    publisher: str | None = Field(default=None, max_length=10_000)
    volume: str | None = Field(default=None, max_length=200)
    issue: str | None = Field(default=None, max_length=200)
    pages: str | None = Field(default=None, max_length=200)
    article_number: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, max_length=100)
    issn: list[str] = Field(default_factory=list, max_length=20)
    isbn: list[str] = Field(default_factory=list, max_length=20)
    authors: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    extra: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=lambda: {"source": "user"})


class CreateItemBody(BaseModel):
    metadata: MetadataBody
    identifiers: list[IdentifierBody] = Field(default_factory=list, max_length=20)
    collection_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    local_overrides: dict[str, Any] = Field(default_factory=dict)


class OverrideBody(BaseModel):
    expected_revision: int = Field(ge=1)
    overrides: dict[str, Any]


class UpdateItemBody(OverrideBody):
    collection_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class RevisionBody(BaseModel):
    expected_revision: int = Field(ge=1)


class BulkItemBody(BaseModel):
    library_item_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class BulkOrganizeBody(BaseModel):
    items: list[BulkItemBody] = Field(min_length=1, max_length=100)
    action: Literal[
        "ADD_COLLECTION",
        "REMOVE_COLLECTION",
        "ADD_TAG",
        "REMOVE_TAG",
        "TRASH",
        "RESTORE",
    ]
    target_id: uuid.UUID | None = None


class CreateCollectionBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_collection_id: uuid.UUID | None = None


class UpdateCollectionBody(CreateCollectionBody):
    expected_revision: int = Field(ge=1)


@router.get("/{library_id}/collections")
async def list_collections(
    library_id: uuid.UUID, session: Database, actor: CurrentActor
) -> dict[str, object]:
    return {"collections": await collection_service.list(session, actor, library_id)}


@router.post("/{library_id}/collections", status_code=status.HTTP_201_CREATED)
async def create_collection(
    library_id: uuid.UUID,
    body: CreateCollectionBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await collection_service.create(
        session,
        actor,
        library_id,
        name=body.name,
        parent_collection_id=body.parent_collection_id,
    )


@router.patch("/{library_id}/collections/{collection_id}")
async def update_collection(
    library_id: uuid.UUID,
    collection_id: uuid.UUID,
    body: UpdateCollectionBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await collection_service.update(
        session,
        actor,
        library_id,
        collection_id,
        name=body.name,
        parent_collection_id=body.parent_collection_id,
        expected_revision=body.expected_revision,
    )


@router.delete("/{library_id}/collections/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    library_id: uuid.UUID,
    collection_id: uuid.UUID,
    expected_revision: int,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> Response:
    await collection_service.remove(
        session,
        actor,
        library_id,
        collection_id,
        expected_revision=expected_revision,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{library_id}/collections/{collection_id}/items/{library_item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def add_item_to_collection(
    library_id: uuid.UUID,
    collection_id: uuid.UUID,
    library_item_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> Response:
    await collection_service.add_item(session, actor, library_id, collection_id, library_item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{library_id}/collections/{collection_id}/items/{library_item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_item_from_collection(
    library_id: uuid.UUID,
    collection_id: uuid.UUID,
    library_item_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> Response:
    await collection_service.remove_item(session, actor, library_id, collection_id, library_item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{library_id}/items")
async def list_items(
    library_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
    item_status: Literal["ACTIVE", "TRASHED"] = Query(default="ACTIVE", alias="status"),
    collection_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=300),
    title: str | None = Query(default=None, max_length=300),
    author: str | None = Query(default=None, max_length=300),
    identifier: str | None = Query(default=None, max_length=500),
    venue: str | None = Query(default=None, max_length=300),
    year_from: int | None = Query(default=None, ge=1000, le=3000),
    year_to: int | None = Query(default=None, ge=1000, le=3000),
    work_type: Annotated[list[str] | None, Query()] = None,
    metadata_source: Annotated[
        list[Literal["UNDEFINED", "CROSSREF", "OPENALEX", "ARXIV", "ZOTERO"]] | None,
        Query(),
    ] = None,
    collection_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
    tag_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
    tag_mode: Literal["ANY", "ALL"] = "ANY",
    include_subcollections: bool = False,
    has_pdf: bool | None = None,
    has_document: bool | None = None,
    has_asset: bool | None = None,
    added_from: date | None = None,
    added_to: date | None = None,
    modified_from: date | None = None,
    modified_to: date | None = None,
    sort: Literal["ADDED", "MODIFIED", "TITLE", "AUTHOR", "YEAR"] = "ADDED",
    direction: Literal["ASC", "DESC"] = "DESC",
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
) -> dict[str, object]:
    items, next_cursor = await catalogue_service.list_items(
        session,
        actor,
        library_id,
        status=item_status,
        collection_id=collection_id,
        tag_id=tag_id,
        query=q,
        title=title,
        author=author,
        identifier=identifier,
        venue=venue,
        year_from=year_from,
        year_to=year_to,
        work_types=work_type or [],
        metadata_sources=metadata_source or [],
        collection_ids=collection_ids or [],
        tag_ids=tag_ids or [],
        tag_mode=tag_mode,
        include_subcollections=include_subcollections,
        has_pdf=has_pdf,
        has_document=has_document,
        has_asset=has_asset,
        added_from=added_from,
        added_to=added_to,
        modified_from=modified_from,
        modified_to=modified_to,
        sort=sort,
        direction=direction,
        limit=limit,
        cursor=cursor,
    )
    return {"items": items, "next_cursor": next_cursor}


@router.post("/{library_id}/items", status_code=status.HTTP_201_CREATED)
async def create_item(
    library_id: uuid.UUID,
    body: CreateItemBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await catalogue_service.create_item(
        session,
        actor,
        library_id,
        metadata=body.metadata.model_dump(),
        identifiers=[value.model_dump() for value in body.identifiers],
        collection_ids=body.collection_ids,
        tag_ids=body.tag_ids,
        local_overrides=body.local_overrides,
    )


@router.post("/{library_id}/items/bulk-organize")
async def bulk_organize_items(
    library_id: uuid.UUID,
    body: BulkOrganizeBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await catalogue_service.bulk_organize(
        session,
        actor,
        library_id,
        entries=[(value.library_item_id, value.expected_revision) for value in body.items],
        action=body.action,
        target_id=body.target_id,
    )


@router.get("/{library_id}/items/{library_item_id}")
async def get_item(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    session: Database,
    actor: CurrentActor,
) -> dict[str, object]:
    return await catalogue_service.get_item(session, actor, library_id, library_item_id)


@router.patch("/{library_id}/items/{library_item_id}")
async def update_item(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    body: UpdateItemBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await catalogue_service.update_item(
        session,
        actor,
        library_id,
        library_item_id,
        overrides=body.overrides,
        collection_ids=body.collection_ids,
        tag_ids=body.tag_ids,
        expected_revision=body.expected_revision,
    )


@router.patch("/{library_id}/items/{library_item_id}/overrides")
async def update_item_overrides(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    body: OverrideBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await catalogue_service.update_overrides(
        session,
        actor,
        library_id,
        library_item_id,
        overrides=body.overrides,
        expected_revision=body.expected_revision,
    )


@router.post("/{library_id}/items/{library_item_id}/trash")
async def trash_item(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    body: RevisionBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await catalogue_service.set_trash_state(
        session,
        actor,
        library_id,
        library_item_id,
        trashed=True,
        expected_revision=body.expected_revision,
    )


@router.post("/{library_id}/items/{library_item_id}/restore")
async def restore_item(
    library_id: uuid.UUID,
    library_item_id: uuid.UUID,
    body: RevisionBody,
    session: Database,
    actor: CurrentActor,
    _: CsrfProtected,
) -> dict[str, object]:
    return await catalogue_service.set_trash_state(
        session,
        actor,
        library_id,
        library_item_id,
        trashed=False,
        expected_revision=body.expected_revision,
    )
