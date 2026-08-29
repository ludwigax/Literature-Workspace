from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ManifestEntry:
    canonical_paper_id: uuid.UUID
    source_artifact_id: uuid.UUID | None
    source_fingerprint: str
    reusable_document_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ReconcilePlan:
    target_manifest_hash: str
    reuse: tuple[ManifestEntry, ...]
    build: tuple[ManifestEntry, ...]
    removed_paper_ids: tuple[uuid.UUID, ...]
    outcome: str


def manifest_hash(
    entries: list[ManifestEntry], pipeline_version_id: uuid.UUID, splitter_config_hash: str
) -> str:
    payload = {
        "pipeline_version_id": str(pipeline_version_id),
        "splitter_config_hash": splitter_config_hash,
        "papers": [
            {
                "canonical_paper_id": str(entry.canonical_paper_id),
                "source_artifact_id": str(entry.source_artifact_id or ""),
                "source_fingerprint": entry.source_fingerprint,
            }
            for entry in sorted(entries, key=lambda value: str(value.canonical_paper_id))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def plan_reconciliation(
    *,
    desired: list[ManifestEntry],
    current_by_paper: dict[uuid.UUID, ManifestEntry],
    pipeline_version_id: uuid.UUID,
    splitter_config_hash: str,
    build_mode: str,
    current_manifest_hash: str | None = None,
) -> ReconcilePlan:
    mode = build_mode.strip().upper()
    if mode not in {"FULL", "UPDATE"}:
        raise ValueError("build_mode must be FULL or UPDATE")
    target_hash = manifest_hash(desired, pipeline_version_id, splitter_config_hash)
    current_ids = set(current_by_paper)
    desired_ids = {entry.canonical_paper_id for entry in desired}
    removed = tuple(sorted(current_ids - desired_ids, key=str))
    if mode == "UPDATE" and current_manifest_hash == target_hash:
        return ReconcilePlan(target_hash, (), (), removed, "NO_CHANGE")
    reuse: list[ManifestEntry] = []
    build: list[ManifestEntry] = []
    for entry in desired:
        current = current_by_paper.get(entry.canonical_paper_id)
        if (
            mode == "UPDATE"
            and current is not None
            and current.reusable_document_id is not None
            and current.source_fingerprint == entry.source_fingerprint
        ):
            reuse.append(
                ManifestEntry(
                    canonical_paper_id=entry.canonical_paper_id,
                    source_artifact_id=entry.source_artifact_id,
                    source_fingerprint=entry.source_fingerprint,
                    reusable_document_id=current.reusable_document_id,
                )
            )
        else:
            build.append(entry)
    return ReconcilePlan(
        target_manifest_hash=target_hash,
        reuse=tuple(reuse),
        build=tuple(build),
        removed_paper_ids=removed,
        outcome="BUILD_REQUIRED",
    )
