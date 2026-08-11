from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, Playwright, Request, Response, Route, sync_playwright

from .api_client import (
    LINK_TREE_PATH,
    SUB_COMMENTS_PATH,
    ApiResponseInfo,
    RequestKey,
    classify_api_url,
    request_key_for_url,
)
from .api_parser import parse_post
from .browser_connection import (
    BrowserConnectionError,
    BrowserMode,
    ensure_edge_debug_browser,
)
from .collector import CommentCollector
from .diagnostics import write_comment_diagnostics
from .dom_parser import parse_dom
from .models import ExportData
from .request_control import (
    CAPTCHA_MARKERS,
    RATE_LIMIT_MARKERS,
    InteractionRequiredError,
    RequestControl,
    RequestState,
    UserAction,
    is_heybox_url,
    rate_limit_message,
)
from .url_parser import ParsedPostUrl
from .utils import sanitize_url_for_log


class BrowserCollectionError(RuntimeError):
    pass


@dataclass
class BrowserOptions:
    mode: BrowserMode = BrowserMode.EDGE
    show_browser: bool = False
    debug: bool = False
    debug_dir: Path = Path("debug")
    login_timeout_seconds: int = 600
    idle_rounds: int = 5
    context_index: int | None = None
    edge_executable: Path | None = None
    request_delay_seconds: float = 2.5
    control: RequestControl | None = None


class BrowserCollector:
    def __init__(self, options: BrowserOptions, logger: logging.Logger):
        self.options = options
        self.logger = logger
        self.top_results: dict[int, dict[str, Any]] = {}
        self.child_results: dict[str, list[dict[str, Any]]] = {}
        self.child_captures: dict[str, list[dict[str, Any]]] = {}
        self.top_raw_files: dict[int, str] = {}
        self.api_statuses: list[str] = []
        self._captured_payload_keys: set[str] = set()
        self._debug_post_dir: Path | None = None
        self.collector: CommentCollector | None = None
        self.control = options.control or RequestControl()
        self._interactive_control = options.control is not None
        self._request_source = "page_native"
        self._last_api_request_at = 0.0
        self._last_probe_url = ""
        self._inflight_keys: set[RequestKey] = set()
        self._successful_keys: set[RequestKey] = set()
        self._duplicate_request_keys: dict[RequestKey, int] = {}
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def collect(self, parsed: ParsedPostUrl) -> ExportData:
        self._reset_capture_state()
        if self.options.debug:
            self._debug_post_dir = self.options.debug_dir / "raw" / parsed.link_id
            self._debug_post_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info("正在解析帖子……")
        self.logger.info("原始链接：%s", parsed.original_url)
        self.logger.info("解析出的帖子 ID：%s", parsed.link_id)
        with sync_playwright() as playwright:
            return self._collect_normal_edge(playwright, parsed)

    def _collect_normal_edge(self, playwright: Playwright, parsed: ParsedPostUrl) -> ExportData:
        try:
            edge = ensure_edge_debug_browser(
                playwright,
                edge_executable=self.options.edge_executable,
                logger=self.logger,
                debug_dir=self.options.debug_dir,
                verify_profile=True,
            )
        except BrowserConnectionError as error:
            raise BrowserCollectionError(str(error)) from error
        self.logger.info("Browser: Microsoft Edge（正常用户 Profile）")
        self.logger.info("Browser version: %s", edge.endpoint.browser_version or "未知")
        self.logger.info("Existing contexts: %s", edge.existing_contexts)
        self.logger.info("Existing pages: %s", edge.existing_pages)
        self.logger.info("Exporter work tab: ready")
        self.logger.info("Heybox login: %s", "yes" if edge.context_status.logged_in else "no")
        if edge.context_status.current_user:
            self.logger.info("Current Heybox user: %s", edge.context_status.current_user)
        if not edge.context_status.logged_in:
            self.logger.info(
                "当前 Edge Profile 尚未确认小黑盒登录状态；程序不会后台轮询登录接口。",
            )
        # connect_over_cdp 仅在任务期间连接；抓取完成、失败或软件退出都只 detach。
        return self._run_context(
            edge.context,
            parsed,
            allow_interaction=True,
            existing_page=edge.page,
        )

    def _reset_capture_state(self) -> None:
        self.top_results.clear()
        self.child_results.clear()
        self.child_captures.clear()
        self.top_raw_files.clear()
        self.api_statuses.clear()
        self._captured_payload_keys.clear()
        self._inflight_keys.clear()
        self._successful_keys.clear()
        self._duplicate_request_keys.clear()
        self._last_api_request_at = 0.0
        self._last_probe_url = ""
        self.collector = None

    def _run_context(
        self,
        context: BrowserContext,
        parsed: ParsedPostUrl,
        *,
        allow_interaction: bool,
        existing_page: Page | None = None,
    ) -> ExportData:
        route_handler = self._route_request
        try:
            self._context = context
            if existing_page is not None:
                page = existing_page
            else:
                page = context.new_page()
            self._page = page
            # Scope interception to the exporter-owned work tab. The user's other
            # normal Edge tabs must never be routed, blocked, navigated, or closed.
            page.route("**/*", route_handler)
            page.on("response", self._capture_response)
            page.on("requestfailed", self._capture_request_failure)
            self.logger.info("默认 API 并发数：1；请求方式：页面官方入口串行触发；最小间隔：%.1f 秒", self.options.request_delay_seconds)

            existing_text = self._local_page_text(page)
            limited_message = rate_limit_message(existing_text)
            if limited_message:
                self._enter_blocked(RequestState.RATE_LIMITED, limited_message)
                self._checkpoint(parsed.canonical_url)

            if parsed.link_id in (page.url or ""):
                with self._source("page_native"):
                    response = page.reload(wait_until="domcontentloaded", timeout=60_000)
            else:
                with self._source("page_native"):
                    response = page.goto(parsed.canonical_url, wait_until="domcontentloaded", timeout=60_000)
            if response:
                self.logger.info("帖子页面 HTTP 状态：%s", response.status)
            self._checkpoint(parsed.canonical_url)
            first = self._wait_for_first_page(page, allow_interaction=allow_interaction)
            link = first.get("link") if isinstance(first.get("link"), dict) else {}
            if not link:
                raise BrowserCollectionError("接口没有返回帖子数据，帖子可能已删除或仅登录用户可见。")
            post = parse_post(link, parsed.canonical_url, parsed.link_id)
            post.displayed_floor_count = first.get("total_floor_num")
            self.collector = CommentCollector(post)
            for page_number in sorted(self.top_results):
                result = self.top_results[page_number]
                self.collector.merge_page(
                    result,
                    page=page_number,
                    is_last=not bool(result.get("has_more_floors")),
                    raw_file=self.top_raw_files.get(page_number, ""),
                )
            for root_id, captures in self.child_captures.items():
                for capture in captures:
                    self.collector.merge_child_page(
                        root_id,
                        capture["result"],
                        lastval=capture["lastval"],
                        request_index=capture["request_index"],
                        raw_file=capture["raw_file"],
                    )
            self.logger.info("已获取原帖正文")
            self.logger.info("正在获取评论……")
            visible_roots = page.locator(".link-comment__comment-item").count()
            returned_roots = len(first.get("comments") or [])
            if first.get("has_more_floors") and returned_roots > visible_roots and visible_roots <= 3:
                if not allow_interaction:
                    raise BrowserCollectionError("未登录页面只展示少量评论，无法触发后续分页")
                self._wait_for_login(page)
            self._load_all_top_comments(page)
            self._load_all_replies(page)
            self._checkpoint(parsed.canonical_url)
            self._expand_long_text(page)
            final_html = page.content()
            try:
                dom_data = parse_dom(final_html, parsed.canonical_url)
                dom_visibility = page.evaluate(
                    """
                    () => Object.fromEntries(
                      [...document.querySelectorAll('.link-comment__comment-item[data-comment-id]')]
                        .map((node) => [String(node.dataset.commentId), Boolean(node.getClientRects().length)])
                    )
                    """
                )
                self.collector.merge_dom(dom_data.comments, dom_visibility)
            except Exception as error:
                self.logger.warning("最终 DOM 评论补充失败，保留结构化 API 结果：%s", error)
            if self.options.debug:
                (self.options.debug_dir / "page.html").write_text(final_html, encoding="utf-8")
                self.logger.info("已保存最终 DOM：%s", (self.options.debug_dir / "page.html").resolve())
            data = self.collector.build()
            data.source["request_audit"] = self.control.audit.as_dict()
            data.source["api_concurrency"] = 1
            data.source["request_mode"] = "serial_page_native"
            data.source["request_delay_seconds"] = self.options.request_delay_seconds
            data.source["duplicate_request_keys_blocked"] = sum(self._duplicate_request_keys.values())
            if self.options.debug:
                csv_path, report_path = write_comment_diagnostics(data, self.options.debug_dir)
                self.logger.info("已保存评论诊断：%s", csv_path.resolve())
                self.logger.info("已保存评论一致性报告：%s", report_path.resolve())
            self.logger.info("已到达评论末尾。")
            self.logger.info(
                "完整性检查：页面显示评论数：%s；实际解析一级评论：%s；楼中楼回复：%s；合计：%s",
                data.statistics.expected_total_comments,
                data.statistics.primary_comments,
                data.statistics.replies,
                data.statistics.total_comments,
            )
            return data
        finally:
            try:
                if self._page is not None:
                    self._page.unroute("**/*", route_handler)
            except Exception:
                pass
            self._context = None
            self._page = None

    @contextmanager
    def _source(self, source: str):
        previous = self._request_source
        self._request_source = source
        try:
            yield
        finally:
            self._request_source = previous

    def _route_request(self, route: Route, request: Request) -> None:
        if not is_heybox_url(request.url):
            route.continue_()
            return
        source = self._request_source
        if self.control.is_blocked:
            self.control.audit.record_blocked_attempt(request.url, source)
            route.abort("blockedbyclient")
            return
        key = request_key_for_url(request.url)
        if key is not None and (key in self._successful_keys or key in self._inflight_keys):
            self._duplicate_request_keys[key] = self._duplicate_request_keys.get(key, 0) + 1
            self.control.audit.record_blocked_attempt(request.url, source)
            self.logger.warning("已阻止重复 API 请求：%s；source=%s", key, source)
            route.abort("blockedbyclient")
            return
        if key is not None:
            delay = self.options.request_delay_seconds - (time.monotonic() - self._last_api_request_at)
            if delay > 0:
                time.sleep(delay)
            self._last_api_request_at = time.monotonic()
            self._last_probe_url = request.url
            self._inflight_keys.add(key)
        self.control.audit.record(request.url, source)
        route.continue_()

    def _capture_request_failure(self, request: Request) -> None:
        key = request_key_for_url(request.url)
        if key is not None:
            self._inflight_keys.discard(key)

    def _local_page_text(self, page: Page) -> str:
        texts: list[str] = []
        for frame in page.frames:
            try:
                text = str(frame.locator("body").inner_text(timeout=1500) or "")
            except Exception:
                continue
            if text:
                texts.append(text)
        return "\n".join(texts)

    def _enter_blocked(self, state: RequestState, message: str = "") -> None:
        changed = self.control.transition(state, message)
        if not changed:
            return
        snapshot = self.control.audit.snapshot()
        last = snapshot["last_request"]
        if state == RequestState.CAPTCHA_REQUIRED:
            self.logger.error("CAPTCHA TRIGGERED")
            self.logger.error("Total requests: %s", snapshot["total_requests"])
            self.logger.error("Last 10 seconds: %s", snapshot["requests_last_10s"])
            self.logger.error("Last 30 seconds: %s", snapshot["requests_last_30s"])
            self.logger.error("Last 60 seconds: %s", snapshot["requests_last_60s"])
            self.logger.error("Request sources: %s", snapshot["by_source"])
            if last is not None:
                self.logger.error("Last request:")
                self.logger.error("endpoint = %s", last.endpoint)
                self.logger.error("source = %s", last.source)
            for index, record in enumerate(snapshot["records"], start=1):
                self.logger.error(
                    "API request before CAPTCHA #%s: time=%s url=%s source=%s",
                    index,
                    record.wall_time.isoformat(timespec="milliseconds"),
                    sanitize_url_for_log(record.url),
                    record.source,
                )
        else:
            self.logger.error("RATE LIMIT TRIGGERED")
            self.logger.error("Page message:")
            self.logger.error("%s", (message or "你的操作过于频繁，请稍后再试")[:500])
            self.logger.error(
                "Requests after CAPTCHA appeared: %s",
                snapshot["requests_after_captcha"],
            )
            source_locations = {
                "page_native": "browser.py:_route_request / 页面官方入口",
                "exporter_fetch": "已删除（旧 browser.py:_reuse_existing_*）",
                "login_check": "browser_connection.py:inspect_context",
                "captcha_check": "browser.py:_single_user_probe",
                "retry": "browser.py:_load_all_top_comments/_load_all_replies",
            }
            for record in snapshot["records_after_captcha"]:
                self.logger.error(
                    "Request after CAPTCHA: time=%s url=%s source=%s code=%s",
                    record.wall_time.isoformat(timespec="milliseconds"),
                    sanitize_url_for_log(record.url),
                    record.source,
                    source_locations.get(record.source, "unknown"),
                )
        self.logger.error("程序已冻结全部小黑盒自动请求；不会轮询、刷新或自动恢复。")

    def _checkpoint(self, fallback_probe_url: str = "") -> None:
        if self.control.state == RequestState.CAPTCHA_REQUIRED and self._page is not None:
            page_text = self._local_page_text(self._page)
            limited = rate_limit_message(page_text)
            if limited:
                self._enter_blocked(RequestState.RATE_LIMITED, limited)
        while self.control.is_blocked:
            if not self._interactive_control:
                state = self.control.state.value
                raise InteractionRequiredError(
                    f"{state}：程序已停止全部小黑盒请求。请使用 GUI 由用户主动确认后再执行一次探测。"
                )
            action = self.control.wait_for_user_action()
            if action == UserAction.CANCEL:
                raise BrowserCollectionError("抓取已由用户取消")
            expected = (
                action == UserAction.CAPTCHA_COMPLETED
                and self.control.state == RequestState.CAPTCHA_REQUIRED
            ) or (
                action == UserAction.RETRY
                and self.control.state == RequestState.RATE_LIMITED
            )
            if not expected:
                continue
            self._single_user_probe(self._last_probe_url or fallback_probe_url)

    def _single_user_probe(self, url: str) -> None:
        if not url or self._context is None:
            self.logger.error("没有可用于单次检查的地址；保持当前阻断状态。")
            return
        previous_state = self.control.state
        self.logger.info("用户已主动确认；只发出 1 次轻量检查：%s", sanitize_url_for_log(url))
        self.control.audit.record(url, "captcha_check")
        try:
            response = self._context.request.get(
                url,
                timeout=30_000,
                max_redirects=0,
                max_retries=0,
            )
            http_status = response.status
            text = response.text()
        except Exception as error:
            self.logger.error("单次检查失败：%s；不会自动重试。", error)
            return

        limited = rate_limit_message(http_status, text)
        if http_status == 429 or limited:
            self._enter_blocked(
                RequestState.RATE_LIMITED,
                limited or "你的操作过于频繁，请稍后再试",
            )
            return

        info = classify_api_url(url)
        if info is not None:
            try:
                payload = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                self.logger.error("单次检查没有返回有效 JSON；保持 %s，且不会自动重试。", previous_state.value)
                return
            if not isinstance(payload, dict):
                self.logger.error("单次检查返回格式无效；保持 %s，且不会自动重试。", previous_state.value)
                return
            status = str(payload.get("status") or "")
            message = str(payload.get("msg") or status)
            limited = rate_limit_message(status, message, payload)
            if limited:
                self._enter_blocked(RequestState.RATE_LIMITED, limited)
                return
            if status == "show_captcha":
                self._enter_blocked(RequestState.CAPTCHA_REQUIRED, message)
                return
            if status != "ok":
                self.logger.error("单次检查仍未通过：%s；保持 %s，且不会自动重试。", message, previous_state.value)
                return
            key = request_key_for_url(url)
            if key is not None:
                self._successful_keys.add(key)
            self._capture_payload(url, http_status, payload)
        else:
            if any(marker in text for marker in RATE_LIMIT_MARKERS):
                self._enter_blocked(RequestState.RATE_LIMITED, text)
                return
            if any(marker in text for marker in CAPTCHA_MARKERS):
                self._enter_blocked(RequestState.CAPTCHA_REQUIRED, "页面仍要求验证码")
                return
        self.control.transition(RequestState.RUNNING, "")
        self.logger.info("单次检查通过，恢复串行低频抓取。")

    def _capture_response(self, response: Response) -> None:
        info = classify_api_url(response.url)
        if not info:
            return
        key = request_key_for_url(response.url)
        if key is not None:
            self._inflight_keys.discard(key)
        safe_url = sanitize_url_for_log(response.url)
        if response.status == 429:
            self._enter_blocked(RequestState.RATE_LIMITED, "HTTP 429：你的操作过于频繁，请稍后再试")
            return
        try:
            payload = response.json()
        except Exception as error:
            self.logger.warning("API HTTP 状态 %s，JSON 解析失败：%s；%s", response.status, error, safe_url)
            return
        if isinstance(payload, dict):
            self._capture_payload(response.url, response.status, payload)

    def _capture_payload(self, url: str, http_status: int, payload: dict[str, Any]) -> None:
        info = classify_api_url(url)
        if not info:
            return
        status = str(payload.get("status") or "")
        message = str(payload.get("msg") or status or "请求失败")
        limited = rate_limit_message(http_status, status, message, payload)
        if http_status == 429 or limited:
            self._enter_blocked(RequestState.RATE_LIMITED, limited or message)
        elif status == "show_captcha":
            self._enter_blocked(RequestState.CAPTCHA_REQUIRED, message)
        payload_key = "|".join((
            info.path,
            str(info.page or ""),
            info.root_comment_id,
            info.lastval,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
        if payload_key in self._captured_payload_keys:
            return
        self._captured_payload_keys.add(payload_key)
        safe_url = sanitize_url_for_log(url)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        self.api_statuses.append(status)
        self.logger.info("API HTTP 状态 %s；status=%s；%s", http_status, status, safe_url)
        raw_file = self._save_raw(info, payload)
        if status != "ok":
            self.logger.warning("小黑盒接口提示：%s", message)
            return
        key = request_key_for_url(url)
        if key is not None:
            self._successful_keys.add(key)
        if info.path == LINK_TREE_PATH:
            page_number = info.page or len(self.top_results) + 1
            self.top_results[page_number] = result
            if raw_file:
                self.top_raw_files[page_number] = raw_file
            comments = result.get("comments") if isinstance(result.get("comments"), list) else []
            if self.collector:
                new_comments, new_replies = self.collector.merge_page(
                    result,
                    page=page_number,
                    is_last=not bool(result.get("has_more_floors")),
                    raw_file=raw_file,
                )
                duplicates = len(comments) - new_comments
                self.logger.info(
                    "评论页 %s：返回 %s，新增一级评论 %s，新增回复 %s，去重 %s，has_more_floors=%s",
                    page_number, len(comments), new_comments, new_replies, max(duplicates, 0), result.get("has_more_floors"),
                )
            else:
                self.logger.info(
                    "评论页 %s：返回 %s，has_more_floors=%s", page_number, len(comments), result.get("has_more_floors")
                )
        elif info.path == SUB_COMMENTS_PATH:
            root_id = info.root_comment_id
            self.child_results.setdefault(root_id, []).append(result)
            request_index = len(self.child_captures.get(root_id, [])) + 1
            capture = {
                "result": result,
                "lastval": info.lastval,
                "request_index": request_index,
                "raw_file": raw_file,
            }
            self.child_captures.setdefault(root_id, []).append(capture)
            comments = result.get("comments") if isinstance(result.get("comments"), list) else []
            added = self.collector.merge_child_page(
                root_id,
                result,
                lastval=info.lastval,
                request_index=request_index,
                raw_file=raw_file,
            ) if self.collector else 0
            self.logger.info(
                "楼中楼 %s：lastval=%s，返回 %s，新增 %s，去重 %s，has_more=%s",
                root_id, info.lastval, len(comments), added, max(len(comments) - added, 0), result.get("has_more"),
            )

    def _save_raw(self, info: ApiResponseInfo, payload: dict[str, Any]) -> str:
        if not self.options.debug or self._debug_post_dir is None:
            return ""
        if info.path == LINK_TREE_PATH:
            name = f"link_tree_page_{info.page or 0}.json"
        else:
            safe_root = re.sub(r"[^0-9A-Za-z_-]", "_", info.root_comment_id)
            request_index = len(self.child_captures.get(info.root_comment_id, [])) + 1
            name = f"sub_comments_{safe_root}_request_{request_index}.json"
        path = self._debug_post_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path.resolve())

    def _wait_for_first_page(self, page: Page, *, allow_interaction: bool) -> dict[str, Any]:
        deadline = time.monotonic() + (self.options.login_timeout_seconds if allow_interaction else 45)
        while time.monotonic() < deadline:
            self._checkpoint()
            if 1 in self.top_results:
                return self.top_results[1]
            if self.api_statuses and self.api_statuses[-1] in {"lack_token", "login", "relogin"}:
                if not allow_interaction:
                    raise BrowserCollectionError("小黑盒要求登录")
                raise BrowserCollectionError("小黑盒登录状态失效。程序不会在后台轮询登录状态，请登录后重新开始。")
            try:
                page.wait_for_timeout(500)
            except Exception as error:
                raise BrowserCollectionError(
                    "正常 Edge 或 Heybox Exporter 工作标签页已关闭。请保持 Edge 打开，然后重新开始导出。"
                ) from error
        raise BrowserCollectionError("等待帖子接口返回超时")

    def _load_all_top_comments(self, page: Page) -> None:
        assert self.collector is not None
        idle = 0
        last_count = len(self.top_results)
        while True:
            self._checkpoint()
            latest = self.top_results[max(self.top_results)]
            if not latest.get("has_more_floors"):
                self.collector.api_reached_end = True
                return
            expected = latest.get("total_floor_num") or self.collector.expected_primary or "?"
            self.logger.info("一级评论：%s / %s；回复：%s", len(self.collector.comments), expected, sum(len(c.replies) for c in self.collector.comments.values()))
            self.logger.info("正在获取下一页……")
            with self._source("retry" if idle else "page_native"):
                self._click_matching(page, ("加载更多评论", "查看更多评论", "更多评论"))
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(max(2500, int(self.options.request_delay_seconds * 1000)))
            self._checkpoint()
            if len(self.top_results) > last_count:
                last_count = len(self.top_results)
                idle = 0
            else:
                idle += 1
            if idle >= self.options.idle_rounds:
                self.collector.mark_fetch_error("top-comments:no-next-page-response")
                self.logger.warning("页面仍有下一页，但连续滚动没有触发新的评论请求；结果将标记为 partial。")
                return

    def _wait_for_login(self, page: Page) -> None:
        self.logger.info("当前帖子需要登录，请在打开的浏览器中完成登录；本步骤只检查本地 DOM，不请求登录接口。")
        deadline = time.monotonic() + self.options.login_timeout_seconds
        while time.monotonic() < deadline:
            self._checkpoint()
            login_button = page.get_by_text("登录", exact=True)
            visible = False
            for index in range(login_button.count()):
                try:
                    if login_button.nth(index).is_visible():
                        visible = True
                        break
                except Exception:
                    pass
            if not visible and page.locator(".link-comment__comment-item").count() > 3:
                page.wait_for_timeout(1500)
                return
            page.wait_for_timeout(500)
        raise BrowserCollectionError("等待用户登录超时")

    def _load_all_replies(self, page: Page) -> None:
        assert self.collector is not None
        failures: list[str] = []
        for comment in list(self.collector.comments.values()):
            self._checkpoint()
            expected = comment.expected_reply_count or 0
            has_more = bool(comment.raw.get("has_more"))
            previous_pages = self.child_results.get(comment.id) or []
            if previous_pages:
                has_more = bool(previous_pages[-1].get("has_more"))
            if not has_more:
                self.collector.child_api_reached_end[comment.id] = True
                continue
            self.logger.info("正在展开 %s 楼的更多回复……", comment.floor or comment.id)
            idle = 0
            while has_more:
                self._checkpoint()
                before = len(comment.replies)
                before_pages = len(self.child_results.get(comment.id) or [])
                selector = f'.link-comment__comment-item[data-comment-id="{comment.id}"] .comment-children__load-all'
                button = page.locator(selector).last
                with self._source("retry" if idle else "page_native"):
                    try:
                        button.scroll_into_view_if_needed(timeout=3000)
                        button.click(timeout=5000)
                    except Exception as normal_click_error:
                        try:
                            # This is the same official DOM action, not an invented API fetch.
                            button.evaluate("(element) => element.click()", timeout=3000)
                        except Exception as dom_click_error:
                            self.logger.warning(
                                "楼中楼 %s 的官方展开入口不可用：%s；%s",
                                comment.id,
                                normal_click_error,
                                dom_click_error,
                            )
                            failures.append(comment.id)
                            self.collector.mark_fetch_error(f"child:{comment.id}")
                            break
                    page.wait_for_timeout(max(2500, int(self.options.request_delay_seconds * 1000)))
                self._checkpoint()
                child_pages = self.child_results.get(comment.id) or []
                received_page = len(child_pages) > before_pages
                if received_page:
                    has_more = bool(child_pages[-1].get("has_more"))
                    self.collector.child_api_reached_end[comment.id] = not has_more
                if len(comment.replies) == before and not received_page:
                    idle += 1
                else:
                    idle = 0
                if idle >= 3:
                    failures.append(comment.id)
                    self.collector.mark_fetch_error(f"child:{comment.id}:no-response")
                    break
        if failures:
            unique = list(dict.fromkeys(failures))
            self.logger.warning("展开失败的评论：%s", ", ".join(unique))

    @staticmethod
    def _click_matching(page: Page, labels: tuple[str, ...]) -> bool:
        for label in labels:
            locator = page.get_by_text(label, exact=True)
            if locator.count():
                try:
                    locator.last.click(timeout=1500)
                    return True
                except Exception:
                    continue
        return False

    def _expand_long_text(self, page: Page) -> None:
        for _ in range(20):
            self._checkpoint()
            clicked = False
            for label in ("全文", "展开全文", "展开更多", "更多"):
                locator = page.get_by_text(label, exact=True)
                for index in range(min(locator.count(), 50)):
                    try:
                        locator.nth(index).click(timeout=500)
                        clicked = True
                    except Exception:
                        pass
            if not clicked:
                return
            page.wait_for_timeout(150)
