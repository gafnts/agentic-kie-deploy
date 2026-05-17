"""
Tests for the NDA extraction schema (src/extractor/schema.py).
"""

import pytest
from pydantic import ValidationError
from schema import NDA, Party


class TestPartyNameNormalizer:
    def test_unicode_curly_single_quotes_normalized(self):
        assert Party(name="O’Brien").name == "O'Brien"
        assert Party(name="‘Foo’").name == "'Foo'"

    def test_unicode_curly_double_quotes_normalized(self):
        assert Party(name="“Foo”").name == '"Foo"'

    def test_commas_stripped(self):
        assert Party(name="Acme Inc.,").name == "Acme_Inc."

    def test_spaces_become_underscores(self):
        assert Party(name="Acme Holdings").name == "Acme_Holdings"

    def test_colons_become_underscores(self):
        assert Party(name="Acme:Subsidiary").name == "Acme_Subsidiary"

    def test_composite_normalization(self):
        assert Party(name="O’Brien, Acme:Co").name == "O'Brien_Acme_Co"

    def test_already_normalized_unchanged(self):
        assert Party(name="Nike_Inc.").name == "Nike_Inc."


class TestNDAEffectiveDate:
    def test_valid_iso_accepted(self):
        assert NDA(effective_date="2024-01-15").effective_date == "2024-01-15"

    def test_none_passes_through(self):
        assert NDA(effective_date=None).effective_date is None

    def test_default_is_none(self):
        assert NDA().effective_date is None

    @pytest.mark.parametrize(
        "bad",
        ["01/15/2024", "2024/01/15", "not-a-date", "2024-13-01"],
    )
    def test_invalid_format_raises(self, bad):
        with pytest.raises(ValidationError):
            NDA(effective_date=bad)


class TestNDAJurisdiction:
    def test_default_is_none(self):
        assert NDA().jurisdiction is None

    def test_none_passes_through(self):
        assert NDA(jurisdiction=None).jurisdiction is None

    def test_bare_name_unchanged(self):
        assert NDA(jurisdiction="Florida").jurisdiction == "Florida"

    def test_spaces_normalized(self):
        assert NDA(jurisdiction="New York").jurisdiction == "New_York"

    def test_colons_normalized(self):
        assert NDA(jurisdiction="Foo:Bar").jurisdiction == "Foo_Bar"

    def test_state_of_prefix_stripped(self):
        assert NDA(jurisdiction="State_of_Delaware").jurisdiction == "Delaware"

    def test_state_of_prefix_after_space_normalization(self):
        assert NDA(jurisdiction="State of Delaware").jurisdiction == "Delaware"

    def test_commonwealth_of_prefix_stripped(self):
        assert NDA(jurisdiction="Commonwealth_of_Virginia").jurisdiction == "Virginia"


class TestNDATerm:
    def test_default_is_none(self):
        assert NDA().term is None

    def test_none_passes_through(self):
        assert NDA(term=None).term is None

    def test_integer_units_accepted(self):
        assert NDA(term="2_years").term == "2_years"

    def test_decimal_units_accepted(self):
        assert NDA(term="1.5_years").term == "1.5_years"

    def test_spaces_normalized_before_validation(self):
        assert NDA(term="2 years").term == "2_years"

    @pytest.mark.parametrize("bad", ["two_years", "11", "_months", "2.years"])
    def test_invalid_format_raises(self, bad):
        with pytest.raises(ValidationError):
            NDA(term=bad)


class TestNDAParty:
    def test_default_is_empty_list(self):
        assert NDA().party == []

    def test_party_list_normalized_per_element(self):
        nda = NDA(party=[Party(name="Acme Inc.,"), Party(name="Nike Inc.")])
        assert [p.name for p in nda.party] == ["Acme_Inc.", "Nike_Inc."]
