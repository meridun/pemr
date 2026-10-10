"""Display-time medication-name casing (issue #209). Every name here is made up."""

import pytest

from pemr.names import display_name


@pytest.mark.parametrize("source, shown", [
    ("ZORBAFEN", "Zorbafen"),                         # ALL-CAPS pharmacy label
    ("zorBAFen", "Zorbafen"),                         # tall-man EHR lettering
    ("zorbafen", "Zorbafen"),
    ("ZORBAFEN-QUILAMOL", "Zorbafen-Quilamol"),       # hyphenated combination
    ("zorbafen/QUILAMOL", "Zorbafen/Quilamol"),
    ("ZORBAFEN (ZORBEX)", "Zorbafen (Zorbex)"),       # parenthesised brand
    ("ZORBAFEN HCL", "Zorbafen HCl"),
    ("QUILAMOL HBR XR", "Quilamol HBr XR"),
    ("ZORBAFEN ODT 5 MG", "Zorbafen ODT 5 mg"),
    ("ZORBAFEN 10MG", "Zorbafen 10mg"),               # glued strength
    ("QUILAMOL 0.5ML", "Quilamol 0.5mL"),
    ("QUILAMOL 250 MCG", "Quilamol 250 mcg"),         # mcg stays mcg, never ug
    ("QUILAMOL 5 MG/ML SOLUTION", "Quilamol 5 mg/mL Solution"),
    ("ZORBAFEN 1% CREAM", "Zorbafen 1% Cream"),
    ("ZORBAFEN, QUILAMOL", "Zorbafen, Quilamol"),
])
def test_casing_rule(source, shown):
    assert display_name(source) == shown


def test_full_label_example_keeps_fixed_forms_and_label_text():
    assert display_name(
        "ZORBAFEN HCL ER 10 MG TABLET (Common brand(s): Zorbex)"
    ) == "Zorbafen HCl ER 10 mg Tablet (Common brand(s): Zorbex)"
    # the label phrase is matched case-insensitively and set back to label text
    assert display_name(
        "ZORBAFEN 10 MG TABLET (COMMON BRAND(S): ZORBEX, QUILEX)"
    ) == "Zorbafen 10 mg Tablet (Common brand(s): Zorbex, Quilex)"


def test_override_wins():
    assert display_name("ZORBAPEN", override="ZorbaPen") == "ZorbaPen"
    assert display_name("ZORBAPEN", override=None) == "Zorbapen"
    assert display_name("ZORBAPEN", override="") == "Zorbapen"


@pytest.mark.parametrize("value", [None, "", "   ", 42])
def test_non_text_passes_through(value):
    assert display_name(value) == value


def test_whitespace_is_preserved():
    assert display_name("  ZORBAFEN  10 MG ") == "  Zorbafen  10 mg "


@pytest.mark.parametrize("source", [
    "ZORBAFEN HCL ER 10 MG TABLET (Common brand(s): Zorbex)",
    "zorBAFen-QUILAMOL 5MG/ML",
    "Zorbafen",
])
def test_idempotent(source):
    once = display_name(source)
    assert display_name(once) == once
