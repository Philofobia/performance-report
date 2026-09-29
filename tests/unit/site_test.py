"""normalize/site.py: one rule for "the site" and "a third party"."""
from normalize.site import in_site, is_third_party, site_scope


def test_scope_drops_www_only():
    assert site_scope("https://www.oakley.com/en-us") == "oakley.com"
    assert site_scope("https://shop.example.co.uk/") == "shop.example.co.uk"


def test_subdomains_are_the_site_lookalikes_are_not():
    assert in_site("https://media.oakley.com/a.jpg", "oakley.com")
    assert in_site("https://oakley.com/", "oakley.com")
    assert not in_site("https://evil-oakley.com/", "oakley.com")
    assert not in_site("https://oakley.com.evil.net/", "oakley.com")
    assert not in_site("https://oakley.com/", "")


def test_third_party_is_relative_to_the_page():
    page = "https://www.oakley.com/en-us"
    assert is_third_party("https://cdn0.forter.com/x.js", page)
    assert not is_third_party("https://assets2.oakley.com/p.png", page)
    assert not is_third_party("data:image/gif;base64,R0lGOD", page)
