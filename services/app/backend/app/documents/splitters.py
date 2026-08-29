from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChunkText:
    content: str
    facet_1: str | None = None
    facet_2: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SplitResult:
    document_content: str
    chunks: tuple[ChunkText, ...]


AdvancedSplitter = Callable[[str, dict[str, Any]], list[ChunkText]]
_SURROGATE = re.compile("[\ud800-\udfff]")


def sanitize_external_text(value: str) -> str:
    """Make untrusted text safe for UTF-8 blobs and PostgreSQL text fields."""
    text = str(value or "").replace("\r\n", "\n").replace("\x00", "")
    return _SURROGATE.sub("\ufffd", text)


def split_output(
    raw_output: str,
    splitter_type: str,
    config: dict[str, Any] | None = None,
    *,
    advanced_splitter: AdvancedSplitter | None = None,
) -> SplitResult:
    text = sanitize_external_text(raw_output).strip()
    if not text:
        raise ValueError("Pipeline output is empty")
    kind = splitter_type.strip().upper()
    options = dict(config or {})
    if kind == "WHOLE":
        chunks = [ChunkText(text)]
        document_content = text
    elif kind == "JSON":
        document_content, chunks = _split_json(text)
    elif kind == "PARAGRAPH":
        document_content, chunks = text, _split_paragraphs(text, options)
    elif kind == "MARKDOWN":
        document_content, chunks = text, _split_markdown(text, options)
    elif kind == "ADVANCED":
        if advanced_splitter is None:
            raise ValueError("ADVANCED splitter requires a trusted backend implementation")
        document_content = text
        chunks = advanced_splitter(text, options)
    else:
        raise ValueError(f"Unsupported splitter_type: {splitter_type}")
    clean = tuple(chunk for chunk in chunks if chunk.content.strip())
    if not clean:
        raise ValueError("Splitter produced no chunks")
    return SplitResult(document_content=document_content, chunks=clean)


def _split_json(text: str) -> tuple[str, list[ChunkText]]:
    match = re.search(r"```\s*json\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    candidate = match.group(1).strip() if match else text
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise ValueError(f"Pipeline output is not valid JSON: {error.msg}") from error
    if isinstance(value, dict):
        chunks = [ChunkText(f"{key}\n{_stringify_json_value(item)}") for key, item in value.items()]
    elif isinstance(value, list):
        chunks = [ChunkText(_stringify_json_value(item)) for item in value]
    else:
        raise ValueError("JSON output must have an object or array at the top level")
    if not chunks:
        raise ValueError("JSON output has no top-level items")
    return json.dumps(value, ensure_ascii=False, indent=2), chunks


def _stringify_json_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _split_paragraphs(text: str, config: dict[str, Any]) -> list[ChunkText]:
    target = int(config.get("chunk_size_words") or 500)
    if target < 1:
        raise ValueError("chunk_size_words must be positive")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if len(words) <= target:
            units.append(paragraph)
        else:
            units.extend(
                " ".join(words[start : start + target]) for start in range(0, len(words), target)
            )
    chunks: list[ChunkText] = []
    current: list[str] = []
    current_words = 0
    for unit in units:
        size = len(unit.split())
        if current and current_words + size > target:
            chunks.append(ChunkText("\n\n".join(current)))
            current, current_words = [], 0
        current.append(unit)
        current_words += size
    if current:
        chunks.append(ChunkText("\n\n".join(current)))
    return chunks


def _split_markdown(text: str, config: dict[str, Any]) -> list[ChunkText]:
    level = int(config.get("heading_level") or 2)
    if not 1 <= level <= 6:
        raise ValueError("heading_level must be between 1 and 6")
    heading = re.compile(rf"^#{{{level}}}(?!#)\s+.+?\s*$")
    chunks: list[ChunkText] = []
    current: list[str] = []
    for line in text.splitlines():
        if heading.match(line) and current and "\n".join(current).strip():
            chunks.append(ChunkText("\n".join(current).strip()))
            current = []
        current.append(line)
    if current and "\n".join(current).strip():
        chunks.append(ChunkText("\n".join(current).strip()))
    return chunks
