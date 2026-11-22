"""Address matching between our cadastre list and a provider's spelling.

A wrong match is worse than a missing one, so there is deliberately no
distance-based or fuzzy fallback.
"""

from __future__ import annotations

import re
import unicodedata


def _fold_accents(s: str) -> str:
    """Drop accents other than the umlauts already handled by the character class below.

    Cadastre and provider spellings disagree on names like Linnegraben/Linnégraben. Four streets in
    the Frankfurt frame are affected and folding them creates no collisions.
    """
    decomposed = unicodedata.normalize("NFD", s)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn" or c == "\u0308")


def norm_street(s: str) -> str:
    s = _fold_accents(s).casefold().replace("ß", "ss")
    s = re.sub(r"strasse|str\.", "str", s)
    return re.sub(r"[^a-z0-9äöü]", "", s)


def norm_number(s: str) -> str:
    return s.casefold().replace(" ", "")


def same_address(street_a: str, number_a: str, street_b: str, number_b: str) -> bool:
    return norm_street(street_a) == norm_street(street_b) and norm_number(number_a) == norm_number(number_b)
