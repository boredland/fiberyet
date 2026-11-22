from fiberyet.match import norm_number, norm_street, same_address


def test_street_spellings_collapse():
    assert norm_street("Bolongarostraße") == norm_street("Bolongarostr.")
    assert norm_street("Alt-Heddernheim") == norm_street("Alt Heddernheim")
    assert norm_street("Hanauer Landstraße") == norm_street("Hanauer Landstr.")


def test_accents_in_street_names_do_not_prevent_a_match():
    # Telekom writes "Linnégraben", the cadastre writes "Linnegraben" (one of the unmatched hold-outs).
    assert norm_street("Zum Linnegraben") == norm_street("Zum Linnégraben")
    assert same_address("Zum Linnegraben", "17d", "Zum Linnégraben", "17D")
    assert norm_street("Cézanneweg") == norm_street("Cezanneweg")
    assert norm_street("Grüne Straße") != norm_street("Grüneburgweg")


def test_ranges_do_not_match_single_numbers():
    assert not same_address("Bolongarostr.", "109-111", "Bolongarostraße", "109")


def test_suffix_case_and_spaces():
    assert norm_number("48 A") == norm_number("48a")
    assert same_address("Gärtnerweg", "48a", "Gärtnerweg", "48A")


def test_different_streets_differ():
    assert not same_address("Zeil", "106", "Zeilweg", "106")
