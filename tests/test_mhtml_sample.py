from pathlib import Path

import pytest

from heybox_exporter.dom_parser import parse_mhtml


def test_real_mhtml_sample() -> None:
    samples = list(Path(__file__).resolve().parents[1].glob("*.mhtml"))
    if not samples:
        pytest.skip("workspace MHTML sample is not present")
    data, document = parse_mhtml(samples[0])
    assert data.post.title == "网上经常有说黑猴后期五六章赶工"
    assert data.post.author.nickname == "坤坤爱游戏"
    assert data.post.author.uid == "34063840"
    assert len(data.post.content) > 250
    assert len(data.post.images) == 1
    assert data.statistics.expected_primary_comments == 104
    assert data.statistics.expected_total_comments == 221
    assert data.statistics.primary_comments == 36
    assert data.statistics.replies == 48
    assert len(data.statistics.incomplete_reply_roots) == 9
    assert len({comment.id for comment in data.comments}) == len(data.comments)
    assert document.resources

