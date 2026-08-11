import logging

from heybox_exporter.api_client import RequestKey, request_key_for_url
from heybox_exporter.browser import BrowserCollector, BrowserOptions
from heybox_exporter.request_control import (
    RequestControl,
    RequestState,
    is_heybox_url,
    rate_limit_message,
)


def test_request_key_ignores_signature_noise_but_keeps_pagination() -> None:
    first = request_key_for_url(
        "https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=123&page=1&hkey=aaa&_t=1"
    )
    duplicate = request_key_for_url(
        "https://api.xiaoheihe.cn/bbs/app/link/tree?_t=2&hkey=bbb&page=1&link_id=123"
    )
    second_page = request_key_for_url(
        "https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=123&page=2"
    )

    assert first == duplicate == RequestKey(
        path="/bbs/app/link/tree", link_id="123", page=1
    )
    assert second_page != first


def test_rate_limit_detection_handles_page_and_api_messages() -> None:
    assert rate_limit_message("你的操作过于频繁，请稍后再试")
    assert rate_limit_message(429, "")
    assert rate_limit_message("status=RATE_LIMITED")
    assert not rate_limit_message("show_captcha")


def test_blocked_state_has_no_timer_and_only_matching_action_unblocks_waiter() -> None:
    control = RequestControl()
    control.audit.record(
        "https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=1&page=1",
        "page_native",
    )
    control.transition(RequestState.CAPTCHA_REQUIRED, "show_captcha")

    assert control.is_blocked
    assert control.submit_retry() is False
    assert control.submit_captcha_completed() is True
    assert control.wait_for_user_action().value == "captcha_completed"
    assert control.state == RequestState.CAPTCHA_REQUIRED
    assert control.audit.as_dict()["requests_after_captcha"] == 0


def test_heybox_host_filter_does_not_match_lookalike_domains() -> None:
    assert is_heybox_url("https://xiaoheihe.cn/a")
    assert is_heybox_url("https://api.xiaoheihe.cn/a")
    assert not is_heybox_url("https://xiaoheihe.cn.example.test/a")


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    def __init__(self) -> None:
        self.continued = False
        self.aborted = False

    def continue_(self) -> None:
        self.continued = True

    def abort(self, _reason: str) -> None:
        self.aborted = True


def test_collector_network_gate_sends_zero_requests_after_captcha() -> None:
    control = RequestControl()
    collector = BrowserCollector(
        BrowserOptions(control=control, request_delay_seconds=0),
        logging.getLogger("network-gate-test"),
    )
    control.transition(RequestState.CAPTCHA_REQUIRED, "show_captcha")
    route = _FakeRoute()

    collector._route_request(  # type: ignore[arg-type]
        route,
        _FakeRequest("https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=1&page=2"),
    )

    audit = control.audit.as_dict()
    assert route.aborted and not route.continued
    assert audit["total_requests"] == 0
    assert audit["requests_after_captcha"] == 0
    assert len(audit["blocked_attempts"]) == 1
