"""Display-time casing for medication names (issue #209).

Medication names arrive in whatever casing their source printed. Pharmacy labels use ALL
CAPS (``ZORBAFEN HCL 10 MG TABLET``); EHR exports use tall-man lettering (``zorBAFen``),
a dispensing-safety cue that marks the part of a name differing from a look-alike drug.
Neither is the drug's real casing, so one person's medication list mixes three styles.

The fix is **display-only**, in the spirit of :mod:`pemr.units`:

  * Stored ``name`` values are never touched. Dedup already lowercases before keying
    (:func:`pemr.dedup.norm`), so casing never forked identity; ``query --json`` keeps the
    source text.
  * :func:`display_name` runs at the render step only, at the medication-line builders
    of ``render summary`` and ``render brief``.

The rule **discards** source capitalisation instead of interpreting it, so there is no
need to detect which convention a source used: each word is lowercased and its first
letter capitalised (generics, parenthesised brands, and each part of a hyphenated or
slashed combination alike). A small set of fixed-form tokens keeps a set spelling --
salt suffixes, release types, strength units -- and pharmacy label phrases such as
``(Common brand(s): ...)`` are cased as label text, not as a name.

The token tables are Python, deliberately **not** a ``dictionary.toml`` section, for the
same reason as the unit registry: the dictionary is user-grown vocabulary and an identity
lever, while these are fixed conventions and a display lever. The one user-grown part --
a per-name display override for names a generic rule cases wrongly (``ZorbaPen``) -- is a
render-only ``[medication_display]`` table loaded by
:func:`pemr.dedup.load_medication_display` and passed in as ``override``.

Unit spellings are **not** routed through :func:`pemr.units.canonical_unit`: that maps
``mcg`` to ``ug``, which would *respell* a label rather than recase it.

Pure: no sqlite, no I/O, and no lookup of any external name service.
"""

from __future__ import annotations

import re

#: Fixed-form tokens, keyed by their lowercase spelling. A word (or a part of a
#: hyphenated/slashed word) matching one takes the set form instead of the casing rule.
_FIXED_FORMS: dict[str, str] = {
    # salt suffixes
    "hcl": "HCl",
    "hbr": "HBr",
    # release types
    "er": "ER",
    "xr": "XR",
    "xl": "XL",
    "sr": "SR",
    "dr": "DR",
    "odt": "ODT",
    # strengths and units
    "mg": "mg",
    "ml": "mL",
    "mcg": "mcg",
    "g": "g",
    "mg/ml": "mg/mL",
    "mcg/ml": "mcg/mL",
    "%": "%",
}

#: Pharmacy label phrases, keyed by their lowercase spelling, emitted in their set form.
#: Matched case-insensitively across whitespace before the word rule runs; text after a
#: phrase (the brand list) is still cased by the word rule.
_LABEL_PHRASES: dict[str, str] = {
    "common brand(s):": "Common brand(s):",
}

_LABEL_RE = re.compile(
    "|".join(
        r"\s+".join(re.escape(part) for part in phrase.split())
        for phrase in _LABEL_PHRASES
    ),
    re.IGNORECASE,
)

_SPACE_SPLIT = re.compile(r"(\s+)")
_PART_SPLIT = re.compile(r"([-/])")
_GLUED = re.compile(r"^(\d+(?:[.,]\d+)?)(.+)$")

_LEADING = "(["
_TRAILING = ")],;:"


def _case_part(part: str) -> str:
    fixed = _FIXED_FORMS.get(part.lower())
    if fixed is not None:
        return fixed
    return part[:1].upper() + part[1:].lower()


def _case_core(core: str) -> str:
    fixed = _FIXED_FORMS.get(core.lower())
    if fixed is not None:
        return fixed
    glued = _GLUED.match(core)
    if glued:
        unit = _FIXED_FORMS.get(glued.group(2).lower())
        if unit is not None:
            return glued.group(1) + unit
    return "".join(
        p if p in "-/" else _case_part(p) for p in _PART_SPLIT.split(core) if p
    )


def _case_word(word: str) -> str:
    start = 0
    while start < len(word) and word[start] in _LEADING:
        start += 1
    end = len(word)
    while end > start and word[end - 1] in _TRAILING:
        end -= 1
    core = word[start:end]
    if not core:
        return word
    return word[:start] + _case_core(core) + word[end:]


def _case_segment(text: str) -> str:
    return "".join(
        tok if not tok or tok.isspace() else _case_word(tok)
        for tok in _SPACE_SPLIT.split(text)
    )


def display_name(name: object, *, override: str | None = None) -> object:
    """The house-style display form of a medication name.

    ``override`` (a per-name ``[medication_display]`` entry) wins when truthy. Non-str or
    blank input is returned unchanged; this never raises. Otherwise source capitalisation
    is discarded and the rule in the module docstring applies:
    ``ZORBAFEN HCL ER 10 MG TABLET (Common brand(s): Zorbex)`` becomes
    ``Zorbafen HCl ER 10 mg Tablet (Common brand(s): Zorbex)``. Idempotent.
    """
    if override:
        return override
    if not isinstance(name, str) or not name.strip():
        return name
    out: list[str] = []
    pos = 0
    for match in _LABEL_RE.finditer(name):
        out.append(_case_segment(name[pos:match.start()]))
        out.append(_LABEL_PHRASES[" ".join(match.group(0).lower().split())])
        pos = match.end()
    out.append(_case_segment(name[pos:]))
    return "".join(out)
