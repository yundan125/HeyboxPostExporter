from heybox_exporter.browser_controller import parse_list_pages, parse_network_requests


def test_parse_list_pages_extracts_urls_and_selection() -> None:
    pages = parse_list_pages(
        """## Pages
1: Example title (https://example.com/path) [selected]
2: https://chatgpt.com/c/example
"""
    )

    assert [page.page_id for page in pages] == [1, 2]
    assert pages[0].title == "Example title"
    assert pages[0].url == "https://example.com/path"
    assert pages[0].selected is True
    assert pages[1].url == "https://chatgpt.com/c/example"
    assert pages[1].selected is False


def test_parse_list_pages_does_not_treat_heading_as_page() -> None:
    assert parse_list_pages("## Pages\nNo pages found") == []


def test_parse_network_requests_keeps_full_query_and_status() -> None:
    requests = parse_network_requests(
        "## Network requests\n"
        "reqid=42 GET https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=1&page=2 [200]\n"
    )
    assert len(requests) == 1
    assert requests[0].request_id == 42
    assert requests[0].url.endswith("link_id=1&page=2")
    assert requests[0].status == "200"
