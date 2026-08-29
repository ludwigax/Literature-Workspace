from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import httpx

from backend.app.models import DocumentPipelineVersion


class PipelineExecutor(Protocol):
    async def execute(
        self,
        *,
        messages: list[dict[str, str]],
        version: DocumentPipelineVersion,
    ) -> str: ...


class UnavailablePipelineExecutor:
    """Fails only when an LLM Pipeline is executed without a configured service."""

    async def execute(
        self,
        *,
        messages: list[dict[str, str]],
        version: DocumentPipelineVersion,
    ) -> str:
        del messages, version
        raise RuntimeError("LITV2_DOCUMENT_PIPELINE_URL is required for LLM Pipelines")


class FakePipelineExecutor:
    """Deterministic executor for acceptance tests; it never calls a network model."""

    def __init__(
        self,
        transform: Callable[[list[dict[str, str]], DocumentPipelineVersion], str] | None = None,
    ) -> None:
        self.transform = transform or (lambda messages, version: messages[-1]["content"])

    async def execute(
        self,
        *,
        messages: list[dict[str, str]],
        version: DocumentPipelineVersion,
    ) -> str:
        return self.transform(messages, version)


class HttpPipelineExecutor:
    """Boundary for a separately deployed Pipeline model service."""

    def __init__(
        self, client: httpx.AsyncClient, *, endpoint: str, timeout_seconds: float = 600
    ) -> None:
        self.client = client
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    async def execute(
        self,
        *,
        messages: list[dict[str, str]],
        version: DocumentPipelineVersion,
    ) -> str:
        response = await self.client.post(
            self.endpoint,
            json={
                "model": version.model,
                "messages": messages,
                "model_config": version.model_config,
                "pipeline_version_id": str(version.pipeline_version_id),
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        output = response.json().get("output_text")
        if not isinstance(output, str) or not output.strip():
            raise RuntimeError("Pipeline model service returned no output_text")
        return output
