"""Address matching between our cadastre list and a provider's spelling.

A wrong match is worse than a missing one, so there is deliberately no
distance-based or fuzzy fallback.
"""

from __future__ import annotations

import re


def norm_street(s: str) -> str:
    s = s.casefold().replace("ß", "ss")
    s = re.sub(r"strasse|str\.", "str", s)
    return re.sub(r"[^a-z0-9äöü]", "", s)


def norm_number(s: str) -> str:
    return s.casefold().replace(" ", "")


def same_address(street_a: str, number_a: str, street_b: str, number_b: str) -> bool:
    return norm_street(street_a) == norm_street(street_b) and norm_number(number_a) == norm_number(number_b)
