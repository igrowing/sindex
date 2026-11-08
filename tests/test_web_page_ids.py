"""The search page is one script over one HTML file: an element the script asks for by id must exist, or the whole
script stops at load and the page does nothing."""

import re

from sindex.web import server as server_module


def _page() -> str:
    return (server_module.STATIC / "index.html").read_text(encoding="utf-8")


def test_every_element_the_page_script_looks_up_exists():
    page = _page()
    html_ids = set(re.findall(r'\bid="([^"]+)"', page))
    asked = set(re.findall(r'\$\("([A-Za-z]+)"\)', page))
    assert asked - html_ids == set()


def test_the_page_has_no_show_only_extension_menu():
    assert not re.search(r"onlyMenu|onlyPanel|only_ext|Show only files", _page())
