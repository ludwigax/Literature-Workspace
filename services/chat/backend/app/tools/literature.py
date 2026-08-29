from __future__ import annotations

import re
from typing import Any

import httpx

from .base import ToolContext, ToolResult, ToolSource


def normalize_doi(value: str) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized)
    normalized = re.sub(r"^doi:\s*", "", normalized)
    if not normalized.startswith("10.") or "/" not in normalized:
        raise ValueError("doi is invalid")
    return normalized


class LiteratureClient:
    def __init__(self, *, base_url: str, timeout: float, service_token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.service_token = service_token

    def _headers(self, context: ToolContext) -> dict[str, str]:
        if not self.service_token:
            raise RuntimeError("Literature service authentication is not configured")
        return {
            "X-Literature-Service-Token": self.service_token,
            "X-Act-As-Principal-Id": str(context.principal_id),
        }

    async def request(
        self,
        method: str,
        path: str,
        context: ToolContext,
        **kwargs: Any,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers=self._headers(context),
        ) as client:
            response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Literature API returned a non-object response")
        return payload


class DocumentRetrievalTool:
    name = "document_retrieval"
    description = (
        "Search one or more Literature document databases and return ranked evidence "
        "chunks, document IDs, paper metadata, and identifiers such as DOI."
    )
    source_type: ToolSource = "FUNCTION"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 100000},
            "database_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string", "description": "Document Database UUID"},
            },
        },
        "required": ["query", "database_ids"],
        "additionalProperties": False,
    }

    def __init__(self, client: LiteratureClient) -> None:
        self.client = client

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        database_ids = arguments.get("database_ids")
        if not isinstance(database_ids, list) or not database_ids:
            raise ValueError("database_ids must not be empty")
        mode = str(context.runtime_config["retrieval_mode"])
        top_k = int(context.runtime_config["retrieval_top_k"])
        chunk_top_k = int(context.runtime_config["chunk_top_k_per_document"])
        payload = await self.client.request(
            "POST",
            "/retrieval/search",
            context,
            json={
                "query": str(arguments.get("query") or ""),
                "databases": [
                    {"database_id": str(database_id), "top_k": top_k, "weight": 1.0}
                    for database_id in database_ids
                ],
                "mode": mode,
                "aggregation": "MAX",
                "database_top_k": top_k,
                "total_top_k": top_k,
                "chunk_top_k_per_document": chunk_top_k,
            },
        )
        return ToolResult(payload)


class DocumentGetByDoiTool:
    name = "document_get_by_doi"
    description = (
        "Resolve an exact DOI in the global canonical-paper catalogue and return its "
        "latest available pipeline document. Never search through a user Library."
    )
    source_type: ToolSource = "FUNCTION"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doi": {"type": "string", "minLength": 4, "maxLength": 500},
        },
        "required": ["doi"],
        "additionalProperties": False,
    }

    def __init__(self, client: LiteratureClient) -> None:
        self.client = client

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        doi = normalize_doi(str(arguments.get("doi") or ""))
        max_chars = int(context.runtime_config["doi_document_max_chars"])
        canonical = await self.client.request(
            "GET",
            "/canonical-papers/by-doi",
            context,
            params={"doi": doi},
        )
        if canonical.get("status") == "NOT_FOUND":
            return ToolResult({"doi": doi, "status": "NOT_FOUND", "documents": []})
        raw_documents = canonical.get("documents")
        if not isinstance(raw_documents, list):
            raise RuntimeError("Canonical DOI lookup returned invalid documents")
        documents: list[dict[str, Any]] = []
        for projection in raw_documents[:1]:
            if not isinstance(projection, dict) or not projection.get("document_id"):
                continue
            document = await self.client.request(
                "GET", f"/documents/{projection['document_id']}", context
            )
            content = str(document.get("content") or "")
            document["content"] = content[:max_chars]
            document["content_truncated"] = len(content) > max_chars
            documents.append(document)
        return ToolResult(
            {
                "doi": doi,
                "canonical_paper_id": canonical.get("canonical_paper_id"),
                "metadata": canonical.get("metadata"),
                "identifiers": canonical.get("identifiers"),
                "status": "FOUND" if documents else "DOCUMENT_UNAVAILABLE",
                "documents": documents,
            }
        )
