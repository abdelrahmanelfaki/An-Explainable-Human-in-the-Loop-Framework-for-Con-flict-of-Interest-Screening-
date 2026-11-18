#!/usr/bin/env python3
"""Offline, deterministic 15-case functional test demonstration for
COI_PubMed_Crossref_Separate_Validator.py.

IMPORTANT: This is a reconstructed suite based on the manuscript's reported
categories, NOT the authors' original historical test suite. The checks do
not call PubMed or Crossref and do not estimate real-world detection accuracy.

Run from the folder containing both files:
    python -m unittest -v test_coi_functional.py
"""
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
CANDIDATES = [
    HERE / "COI_PubMed_Crossref_Separate_Validator(1).py",
    HERE / "COI_PubMed_Crossref_Separate_Validator.py",
]
SOURCE = next((p for p in CANDIDATES if p.is_file()), None)
if SOURCE is None:
    raise FileNotFoundError("Place the validation program next to this test file.")
spec = importlib.util.spec_from_file_location("coi_validator_under_test", SOURCE)
coi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coi)


class TestAuthorMatching(unittest.TestCase):
    # 7 author-name matching tests: positive and negative
    def test_01_exact_full_name(self):
        self.assertTrue(coi.author_match("John Smith", "John Smith"))

    def test_02_initials_match_full_name(self):
        self.assertTrue(coi.author_match("J Smith", "John Smith"))

    def test_03_accent_normalization(self):
        self.assertTrue(coi.author_match("José García", "Jose Garcia"))

    def test_04_surname_first_name(self):
        self.assertTrue(coi.author_match("Smith, John", "John Smith"))

    def test_05_hyphenated_surname_first(self):
        self.assertTrue(coi.author_match("Smith-Jones, Mary", "Mary Smith-Jones"))

    def test_06_reject_different_surname(self):
        self.assertFalse(coi.author_match("John Smith", "John Jones"))

    def test_07_reject_different_initial(self):
        self.assertFalse(coi.author_match("John Smith", "Peter Smith"))


class TestOrganizationMatching(unittest.TestCase):
    # 3 organization-matching tests
    def test_08_identical_organization(self):
        self.assertTrue(coi.orgmatch("Eli Lilly and Company", "Eli Lilly and Company"))

    def test_09_organization_in_longer_affiliation(self):
        self.assertTrue(coi.orgmatch("University of Tabuk", "Department of Computer Science, University of Tabuk, Saudi Arabia"))

    def test_10_reject_different_organization(self):
        self.assertFalse(coi.orgmatch("University of Tabuk", "University of Jeddah"))


class TestIdentifiersAndInputs(unittest.TestCase):
    def test_11_doi_canonicalization(self):
        self.assertEqual(coi.canonical_doi("https://doi.org/10.1000/ABC.123)."), "10.1000/abc.123")

    def test_12_input_column_alias_handling(self):
        record = coi.normal_record({"PubMed_ID": "12345678", "Author_Name": "J Smith", "Organization_Name": "Example University"})
        self.assertEqual((record["pmid"], record["author"], record["organization"]),
                         ("12345678", "J Smith", "Example University"))

    def test_13_excludes_index_pmid_and_doi(self):
        row = {"pmid": "12345678", "doi": "", "evidence_pmids": "", "evidence_dois": ""}
        index = {"doi": "https://doi.org/10.1000/ABC", "year": "2021"}
        excluded_pmids, excluded_dois = coi.exclude_set(row, index)
        self.assertIn("12345678", excluded_pmids)
        self.assertIn("10.1000/abc", excluded_dois)

    def test_14_excludes_explicit_supporting_ids(self):
        row = {"pmid": "12345678", "evidence_pmids": "34567890; 45678901", "evidence_dois": "10.1000/ABC;https://doi.org/10.1000/XYZ"}
        index = {"doi": "10.1000/INDEX"}
        excluded_pmids, excluded_dois = coi.exclude_set(row, index)
        self.assertTrue({"34567890", "45678901"}.issubset(excluded_pmids))
        self.assertTrue({"10.1000/abc", "10.1000/xyz"}.issubset(excluded_dois))

    def test_15_pubmed_author_query_construction(self):
        # Network-free test: mock the API helper, inspect parameters, and return an empty hit list.
        captured = []

        def fake_get(url, params=None, kind="json"):
            captured.append((url, params, kind))
            return {"esearchresult": {"idlist": [], "count": "0"}}

        row = {"pmid": "12345678", "author": "John Smith", "organization": "University of Tabuk", "evidence_pmids": "", "evidence_dois": ""}
        index = {"doi": "10.1000/index", "year": "2023"}
        with patch.object(coi, "get", side_effect=fake_get):
            result = coi.verify_pubmed(row, index, "researcher@example.com", limit=180, year_window=5)
        self.assertEqual(result["status"], "NOT_OBSERVED")
        self.assertEqual(len(captured), 1)
        url, params, kind = captured[0]
        self.assertTrue(url.endswith("esearch.fcgi"))
        self.assertIn('"smith j"[Author]', params["term"])
        self.assertIn('2018:2028[dp]', params["term"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
