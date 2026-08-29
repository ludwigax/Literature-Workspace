from backend.app.ingestion.citations import parse_citation_file


def test_bibtex_ris_and_csl_json_share_limited_record_shape() -> None:
    bibtex = b"""@article{ada2025,
      title={A BibTeX Paper},
      author={Lovelace, Ada and Hopper, Grace},
      year={2025}, journal={Journal A}, doi={10.1000/BIB}
    }"""
    ris = b"""TY  - JOUR
TI  - An RIS Paper
AU  - Turing, Alan
PY  - 2024
JO  - Journal B
DO  - https://doi.org/10.1000/RIS
ER  -
"""
    csl = b"""[{"id":"csl-1","title":"A CSL Paper","DOI":"10.1000/CSL",
      "author":[{"given":"Katherine","family":"Johnson"}],
      "issued":{"date-parts":[[2023]]},"container-title":"Journal C"}]"""

    bib_record = parse_citation_file(bibtex, "records.bib")[0]
    ris_record = parse_citation_file(ris, "records.ris")[0]
    csl_record = parse_citation_file(csl, "records.json")[0]

    assert (bib_record.title, bib_record.doi, bib_record.publication_year) == (
        "A BibTeX Paper",
        "10.1000/bib",
        2025,
    )
    assert (ris_record.title, ris_record.doi, ris_record.publication_year) == (
        "An RIS Paper",
        "10.1000/ris",
        2024,
    )
    assert (csl_record.title, csl_record.doi, csl_record.publication_year) == (
        "A CSL Paper",
        "10.1000/csl",
        2023,
    )
