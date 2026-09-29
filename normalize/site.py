"""Which hosts belong to the site under test, and which are third parties.

One definition, used wherever the difference matters: the runner sends the
bot-allowlist header to the site only and can block third-party scripts, and
the HAR analysis splits a page's cost into the site's own and everyone else's.
A second, slightly different rule in either place would let a request be
"first party" to one and "third party" to the other.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def site_scope(page_url: str) -> str:
    """The site a page belongs to: its host, minus ``www.``.

    ``www.oakley.com`` scopes to ``oakley.com``, which admits
    ``media.oakley.com`` and ``assets2.oakley.com`` — the site's own image and
    script hosts. Deliberately not a registrable-domain guess: stripping more
    than ``www.`` would turn ``shop.example.co.uk`` into ``co.uk`` and make
    every site under it first party. Too narrow is visible (a site host listed
    as a third party); too broad hides third parties.
    """
    host = (urlsplit(page_url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def in_site(request_url: str, scope: str) -> bool:
    """Whether ``request_url`` is ``scope`` itself or one of its subdomains."""
    host = (urlsplit(request_url).hostname or "").lower()
    return bool(scope) and (host == scope or host.endswith("." + scope))


def is_third_party(request_url: str, page_url: str) -> bool:
    """A request to a host outside the page's site. ``data:`` URLs are neither."""
    if not urlsplit(request_url).hostname:
        return False
    return not in_site(request_url, site_scope(page_url))
