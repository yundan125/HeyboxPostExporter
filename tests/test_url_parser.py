from heybox_exporter.url_parser import InvalidXiaoheiheUrl, parse_post_url


def test_canonical_url() -> None:
    parsed = parse_post_url("https://www.xiaoheihe.cn/app/bbs/link/187672249")
    assert parsed.link_id == "187672249"
    assert parsed.canonical_url.endswith("/187672249")


def test_share_url() -> None:
    parsed = parse_post_url("https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?foo=1&link_id=63426002")
    assert parsed.link_id == "63426002"


def test_nested_redirect_data() -> None:
    parsed = parse_post_url(
        "https://www.xiaoheihe.cn/app/bbs/link/abc123?redirect_data=%7B%22link_id%22%3A%22999%22%7D"
    )
    assert parsed.link_id == "abc123"


def test_rejects_other_domain() -> None:
    try:
        parse_post_url("https://example.com/?link_id=1")
    except InvalidXiaoheiheUrl:
        return
    raise AssertionError("other domains must be rejected")

