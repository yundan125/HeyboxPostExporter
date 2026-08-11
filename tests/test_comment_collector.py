from heybox_exporter.api_parser import parse_post
from heybox_exporter.collector import CommentCollector
from heybox_exporter.utils import canonical_comment_id


def _wrapper(comment_id: int | str, *, pinned: int = 0, child_num: int = 0, has_more: int = 0) -> dict:
    return {
        "comment": [{
            "commentid": comment_id,
            "userid": 1,
            "text": f"comment-{comment_id}",
            "floor_num": 1,
            "is_top": pinned,
            "child_num": child_num,
            "has_more": has_more,
            "user": {"username": "tester"},
        }]
    }


def test_canonical_comment_id_unifies_numeric_and_string_ids() -> None:
    assert canonical_comment_id({"commentid": 926239413}) == "926239413"
    assert canonical_comment_id({"comment_id": "926239413"}) == "926239413"


def test_pinned_and_counted_not_returned_are_complete_visible() -> None:
    post = parse_post({"linkid": 1, "comment_num": 3}, "https://example.test/1", "1")
    collector = CommentCollector(post)
    collector.merge_page(
        {
            "total_floor_num": 1,
            "has_more_floors": 0,
            "link": {"comment_num": 3},
            "comments": [
                _wrapper(10, pinned=1, child_num=1, has_more=1),
                _wrapper("11"),
            ],
        },
        page=1,
        is_last=True,
    )
    collector.merge_child_page("10", {"comments": [], "has_more": False, "lastval": ""}, lastval="x")

    data = collector.build()

    assert [item.id for item in data.comments] == ["10", "11"]
    assert data.statistics.expected_primary_comments == 1
    assert data.statistics.pinned_primary_comments == 1
    assert data.statistics.counted_but_not_returned_roots == {"10": 1}
    assert data.statistics.server_unavailable_comments == 1
    assert data.statistics.completeness == "complete_visible"
