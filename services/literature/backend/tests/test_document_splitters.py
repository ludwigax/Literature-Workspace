from __future__ import annotations

import pytest

from backend.app.documents.splitters import ChunkText, sanitize_external_text, split_output


def test_external_text_sanitizer_removes_sql_nul_and_replaces_surrogates() -> None:
    value = sanitize_external_text("alpha\x00\r\nbeta\ud800\tγ\n")

    assert value == "alpha\nbeta�\tγ\n"
    assert value.encode("utf-8").decode("utf-8") == value


def test_splitter_sanitizes_before_document_and_chunk_creation() -> None:
    result = split_output("one\x00 two\r\n\r\nthree\udfff", "PARAGRAPH")

    assert result.document_content == "one two\n\nthree�"
    assert [chunk.content for chunk in result.chunks] == ["one two\n\nthree�"]


def test_json_dict_uses_key_newline_value_without_hidden_source_key() -> None:
    result = split_output(
        'before\n```Json\n{"preparation": {"solvent": "water"}, "score": 4}\n```\nafter',
        "JSON",
    )

    assert [chunk.content for chunk in result.chunks] == [
        'preparation\n{"solvent":"water"}',
        "score\n4",
    ]
    assert all(chunk.attributes == {} for chunk in result.chunks)


def test_json_list_stringifies_each_top_level_item() -> None:
    result = split_output('[{"name":"a"}, "plain"]', "JSON")

    assert [chunk.content for chunk in result.chunks] == ['{"name":"a"}', "plain"]


def test_markdown_splits_only_at_the_exact_heading_level() -> None:
    result = split_output(
        "# Title\nintro\n### Detail\nstill intro\n## First\na\n### Nested\nb\n## Second\nc",
        "MARKDOWN",
        {"heading_level": 2},
    )

    assert [chunk.content for chunk in result.chunks] == [
        "# Title\nintro\n### Detail\nstill intro",
        "## First\na\n### Nested\nb",
        "## Second\nc",
    ]


def test_paragraph_splitter_preserves_boundaries_and_splits_oversized_paragraphs() -> None:
    result = split_output(
        "one two\n\nthree four five\n\nsix seven eight nine ten eleven",
        "PARAGRAPH",
        {"chunk_size_words": 5},
    )

    assert [chunk.content for chunk in result.chunks] == [
        "one two\n\nthree four five",
        "six seven eight nine ten",
        "eleven",
    ]


def test_advanced_splitter_is_backend_supplied_and_can_set_facets() -> None:
    with pytest.raises(ValueError, match="trusted backend"):
        split_output("source", "ADVANCED")

    result = split_output(
        "source",
        "ADVANCED",
        {"mode": "demo"},
        advanced_splitter=lambda text, config: [
            ChunkText(text, facet_1="preparation", facet_2=config["mode"])
        ],
    )
    assert result.chunks[0].facet_1 == "preparation"
    assert result.chunks[0].facet_2 == "demo"


@pytest.mark.parametrize("kind", ["WHOLE", "JSON", "PARAGRAPH", "MARKDOWN"])
def test_empty_output_is_rejected(kind: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        split_output("  ", kind)
