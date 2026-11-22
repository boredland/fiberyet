from fiberyet.match import norm_number, norm_street, same_address


def test_street_spellings_collapse():
    assert norm_street("Bolongarostraße") == norm_street("Bolongarostr.")
    assert norm_street("Alt-Heddernheim") == norm_street("Alt Heddernheim")
    assert norm_street("Hanauer Landstraße") == norm_street("Hanauer Landstr.")


def test_ranges_do_not_match_single_numbers():
    assert not same_address("Bolongarostr.", "109-111", "Bolongarostraße", "109")


def test_suffix_case_and_spaces():
    assert norm_number("48 A") == norm_number("48a")
    assert same_address("Gärtnerweg", "48a", "Gärtnerweg", "48A")


def test_different_streets_differ():
    assert not same_address("Zeil", "106", "Zeilweg", "106")
