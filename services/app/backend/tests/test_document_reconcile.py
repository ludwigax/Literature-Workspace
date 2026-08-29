from __future__ import annotations

import uuid

from backend.app.documents.reconcile import ManifestEntry, manifest_hash, plan_reconciliation


def entry(paper: uuid.UUID, fingerprint: str, document: uuid.UUID | None = None) -> ManifestEntry:
    return ManifestEntry(paper, uuid.uuid4(), fingerprint, document)


def test_update_reuses_unchanged_builds_added_and_omits_removed() -> None:
    version = uuid.uuid4()
    kept, added, removed = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old_document = uuid.uuid4()
    current = {
        kept: entry(kept, "same", old_document),
        removed: entry(removed, "old", uuid.uuid4()),
    }

    plan = plan_reconciliation(
        desired=[entry(kept, "same"), entry(added, "new")],
        current_by_paper=current,
        pipeline_version_id=version,
        splitter_config_hash="splitter",
        build_mode="UPDATE",
    )

    assert [item.canonical_paper_id for item in plan.reuse] == [kept]
    assert plan.reuse[0].reusable_document_id == old_document
    assert [item.canonical_paper_id for item in plan.build] == [added]
    assert plan.removed_paper_ids == (removed,)


def test_source_change_rebuilds_only_that_paper() -> None:
    version = uuid.uuid4()
    changed, stable = uuid.uuid4(), uuid.uuid4()
    current = {
        changed: entry(changed, "before", uuid.uuid4()),
        stable: entry(stable, "same", uuid.uuid4()),
    }
    plan = plan_reconciliation(
        desired=[entry(changed, "after"), entry(stable, "same")],
        current_by_paper=current,
        pipeline_version_id=version,
        splitter_config_hash="splitter",
        build_mode="UPDATE",
    )

    assert [item.canonical_paper_id for item in plan.build] == [changed]
    assert [item.canonical_paper_id for item in plan.reuse] == [stable]


def test_full_build_never_reuses_and_daily_identical_update_is_no_change() -> None:
    version = uuid.uuid4()
    paper = uuid.uuid4()
    desired = [entry(paper, "same")]
    current = {paper: entry(paper, "same", uuid.uuid4())}
    target = manifest_hash(desired, version, "splitter")

    no_change = plan_reconciliation(
        desired=desired,
        current_by_paper=current,
        pipeline_version_id=version,
        splitter_config_hash="splitter",
        build_mode="UPDATE",
        current_manifest_hash=target,
    )
    full = plan_reconciliation(
        desired=desired,
        current_by_paper=current,
        pipeline_version_id=version,
        splitter_config_hash="splitter",
        build_mode="FULL",
        current_manifest_hash=target,
    )

    assert no_change.outcome == "NO_CHANGE"
    assert not no_change.build
    assert full.outcome == "BUILD_REQUIRED"
    assert [item.canonical_paper_id for item in full.build] == [paper]
    assert not full.reuse


def test_pipeline_version_is_part_of_manifest_identity() -> None:
    paper = uuid.uuid4()
    desired = [entry(paper, "same")]

    assert manifest_hash(desired, uuid.uuid4(), "splitter") != manifest_hash(
        desired, uuid.uuid4(), "splitter"
    )
