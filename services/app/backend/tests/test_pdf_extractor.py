from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from backend.app.ingestion.identifiers import (
    extract_arxiv_ids,
    select_pdf_identifiers,
)
from backend.app.ingestion.pdf_import import PypdfTextExtractor
from backend.app.ingestion.providers import extract_dois
from backend.app.papers.service import paper_service


@pytest.mark.asyncio
async def test_pypdf_extractor_reads_doi_from_pdf_text_layer() -> None:
    output = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 720 Td (doi:10.1000/PDF-TEXT) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(output)

    extracted = await PypdfTextExtractor(max_pages=12).extract(output.getvalue())

    assert extracted.page_count == 1
    assert extract_dois(extracted.text) == ["10.1000/pdf-text"]


@pytest.mark.asyncio
async def test_pdf_metadata_dictionary_wins_over_first_page() -> None:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_metadata({"/Subject": "Published as doi:10.1000/METADATA"})
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 720 Td (doi:10.1000/PAGE) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(output)

    extracted = await PypdfTextExtractor(max_pages=2).extract(output.getvalue())
    selected = select_pdf_identifiers(extracted.metadata_text, extracted.page_text)

    assert selected.evidence_source == "PDF_METADATA"
    assert [(value.scheme, value.normalized_value) for value in selected.identifiers] == [
        ("DOI", "10.1000/metadata")
    ]


def test_arxiv_identifier_extraction_handles_modern_legacy_and_datacite_doi() -> None:
    text = "arXiv:2401.12345v2; arXiv:hep-th/9901001v3; https://doi.org/10.48550/arXiv.2401.12345"

    assert extract_arxiv_ids(text) == ["2401.12345", "hep-th/9901001"]
    selected = select_pdf_identifiers("", text)
    assert {(value.scheme, value.normalized_value) for value in selected.identifiers} == {
        ("DOI", "10.48550/arxiv.2401.12345"),
        ("ARXIV", "2401.12345"),
    }


def test_arxiv_filename_is_a_last_resort_identifier_source() -> None:
    selected = select_pdf_identifiers("", "", "2603.09202v2.pdf")

    assert selected.evidence_source == "FILENAME"
    assert selected.metadata_doi == "10.48550/arxiv.2603.09202"
    assert {(value.scheme, value.normalized_value) for value in selected.identifiers} == {
        ("DOI", "10.48550/arxiv.2603.09202"),
        ("ARXIV", "2603.09202"),
    }


def test_manual_datacite_arxiv_doi_adds_arxiv_identifier_alias() -> None:
    identifiers = paper_service.normalize_identifiers(
        [{"scheme": "DOI", "value": "https://doi.org/10.48550/arXiv.2401.12345v2"}]
    )

    assert {(scheme, normalized) for scheme, normalized, _ in identifiers} == {
        ("DOI", "10.48550/arxiv.2401.12345"),
        ("ARXIV", "2401.12345"),
    }
