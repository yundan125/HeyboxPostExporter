from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from .api_client import LINK_TREE_PATH, SUB_COMMENTS_PATH, classify_api_url, request_key_for_url
from .api_parser import parse_post
from .browser_controller import BrowserPage, ChromeDevToolsMcpController
from .collector import CommentCollector
from .dom_parser import parse_dom
from .models import ExportData
from .request_control import (
    CAPTCHA_MARKERS,
    RATE_LIMIT_MARKERS,
    InteractionRequiredError,
    RequestControl,
    RequestState,
    UserAction,
    rate_limit_message,
)
from .url_parser import ParsedPostUrl


class McpCollectionError(RuntimeError):
    pass


class McpBrowserCollector:
    """Collect Heybox data through one chrome-devtools-mcp sidecar.

    API URLs are never replayed. The collector reads response bodies already
    produced by the page, then asks the page to scroll or click its own controls.
    """

    def __init__(
        self,
        controller: ChromeDevToolsMcpController,
        logger: logging.Logger,
        *,
        control: RequestControl | None = None,
        debug: bool = False,
    ) -> None:
        self.controller = controller
        self.logger = logger
        self.control = control or RequestControl()
        self.interactive = control is not None
        self.debug = debug
        self.seen_request_keys: set[object] = set()
        self.seen_request_ids: set[int] = set()
        self.top_results: dict[int, dict[str, Any]] = {}
        self.child_results: dict[str, list[dict[str, Any]]] = {}
        self.raw_payloads: list[dict[str, Any]] = []
        self.collector: CommentCollector | None = None
        self.selected_page_id: int | None = None
        self.parsed: ParsedPostUrl | None = None
        self.reconnect_attempted = False

    def collect(self, parsed: ParsedPostUrl) -> ExportData:
        self.parsed = parsed
        self.logger.info("正在读取帖子……")
        self._select_work_page(parsed)
        with tempfile.TemporaryDirectory(prefix="heybox-mcp-") as capture_dir:
            capture_path = Path(capture_dir)
            try:
                first = self._wait_for_first_result(capture_path)
            except McpCollectionError:
                return self._collect_dom_fallback(parsed)
            link = first.get("link") if isinstance(first.get("link"), dict) else {}
            if not link:
                raise McpCollectionError("接口没有返回帖子数据，帖子可能已删除或仅登录用户可见。")
            post = parse_post(link, parsed.canonical_url, parsed.link_id)
            post.displayed_floor_count = first.get("total_floor_num")
            self.collector = CommentCollector(post)
            for page_number in sorted(self.top_results):
                result = self.top_results[page_number]
                self.collector.merge_page(result, page=page_number, is_last=not bool(result.get("has_more_floors")))
            for root_id, pages in self.child_results.items():
                for index, result in enumerate(pages, start=1):
                    self.collector.merge_child_page(root_id, result, request_index=index)

            self.logger.info("已获取原帖正文")
            self.logger.info("正在加载评论")
            self._load_all_primary(capture_path)
            self._load_all_replies(capture_path)
            self._expand_long_text()
            self._checkpoint()

            try:
                html = self.controller.get_page_html()
                dom_data = parse_dom(html, parsed.canonical_url)
                self.collector.merge_dom(dom_data.comments)
            except Exception as error:
                self.logger.warning("DOM 补充失败，保留结构化 API 数据：%s", error)

            data = self.collector.build()
            data.source.update({
                "mode": "chrome_devtools_mcp",
                "mcp_version": self.controller.mcp_version,
                "request_mode": "serial_page_native",
                "api_concurrency": 1,
                "request_audit": self.control.audit.as_dict(),
                "child_requests": self.collector.child_request_traces,
            })
            if self.debug:
                data.source["raw_payloads"] = self.raw_payloads
            self.logger.info("已获取 %s 条一级评论", data.statistics.primary_comments)
            self.logger.info("已获取 %s 条回复", data.statistics.replies)
            return data

    def _select_work_page(self, parsed: ParsedPostUrl) -> None:
        pages = self.controller.list_pages()
        exact = next((page for page in pages if self._page_matches(page, parsed.link_id)), None)
        if exact is not None:
            self.controller.select_page(exact.page_id, bring_to_front=False)
            self.selected_page_id = exact.page_id
            self.controller.work_page_id = exact.page_id
            self.logger.info("找到目标帖子，直接复用现有页面（不刷新）")
            return

        reusable = None
        if self.controller.work_page_id is not None:
            reusable = next((p for p in pages if p.page_id == self.controller.work_page_id), None)
        if reusable is not None:
            self.controller.select_page(reusable.page_id, bring_to_front=False)
            self._checkpoint(read_page=False)
            self.controller.navigate(parsed.canonical_url, timeout_ms=60_000)
            self.selected_page_id = reusable.page_id
            self.logger.info("正在使用 Heybox Exporter 工作标签页打开目标帖子")
            return

        self.controller.create_page(parsed.canonical_url, background=False, timeout_ms=60_000)
        self.selected_page_id = self.controller.selected_page_id
        self.logger.info("已创建 Heybox Exporter 工作标签页")
        try:
            self.controller.evaluate("() => { window.name = 'HeyboxPostExporterWorkPage'; return true; }")
        except Exception:
            pass

    @staticmethod
    def _page_matches(page: BrowserPage, link_id: str) -> bool:
        try:
            from .url_parser import parse_post_url
            return parse_post_url(page.url).link_id == link_id
        except ValueError:
            return False

    def _wait_for_first_result(self, capture_dir: Path) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        scrolled = False
        while time.monotonic() < deadline:
            self._checkpoint()
            self._drain_network(capture_dir)
            if self.top_results:
                return self.top_results[min(self.top_results)]
            if not scrolled and time.monotonic() > deadline - 12:
                self.controller.scroll()
                scrolled = True
            time.sleep(0.5)
        raise McpCollectionError("等待帖子接口返回超时。请确认帖子页面可正常打开并已登录。")

    def _collect_dom_fallback(self, parsed: ParsedPostUrl) -> ExportData:
        """Preserve an already-open page without refreshing when old network bodies are gone."""
        self.logger.info("现有页面的历史响应体不可用，改用已渲染页面并继续正常展开")
        for _ in range(8):
            self._checkpoint()
            clicked = self.controller.evaluate_json(
                "() => { const labels=['加载更多评论','查看更多评论','更多评论','查看全部回复','查看更多回复']; "
                "let count=0; for(const n of document.querySelectorAll('button,a,div')) { "
                "const t=(n.textContent||'').trim(); if(n.offsetParent && labels.some(x=>t.includes(x))) { n.click(); count++; } } "
                "window.scrollTo(0,document.documentElement.scrollHeight); return count; }"
            )
            if not clicked:
                break
            time.sleep(1)
        data = parse_dom(self.controller.get_page_html(), parsed.canonical_url)
        data.source.update({
            "mode": "chrome_devtools_mcp_dom_fallback",
            "mcp_version": self.controller.mcp_version,
            "request_mode": "page_native_dom",
            "request_audit": self.control.audit.as_dict(),
        })
        if data.statistics.completeness == "unknown":
            data.statistics.completeness = "partial"
        self.logger.info("已从页面读取 %s 条一级评论和 %s 条回复", data.statistics.primary_comments, data.statistics.replies)
        return data

    def _load_all_primary(self, capture_dir: Path) -> None:
        assert self.collector is not None
        idle = 0
        while True:
            self._checkpoint()
            latest = self.top_results[max(self.top_results)]
            if not latest.get("has_more_floors"):
                self.collector.api_reached_end = True
                return
            before = len(self.seen_request_keys)
            replies = sum(len(item.replies) for item in self.collector.comments.values())
            self.logger.info("正在加载评论：一级评论 %s，回复 %s", len(self.collector.comments), replies)
            self.controller.evaluate(
                "() => { const labels=['加载更多评论','查看更多评论','更多评论']; "
                "const nodes=[...document.querySelectorAll('button,a,div')]; "
                "const node=nodes.reverse().find(n=>n.offsetParent && labels.includes((n.textContent||'').trim())); "
                "if(node) node.click(); window.scrollTo(0,document.documentElement.scrollHeight); return !!node; }"
            )
            self._wait_network_stable(capture_dir)
            idle = 0 if len(self.seen_request_keys) > before else idle + 1
            if idle >= 5:
                self.collector.mark_fetch_error("top-comments:no-next-page-response")
                return

    def _load_all_replies(self, capture_dir: Path) -> None:
        assert self.collector is not None
        roots = list(self.collector.comments.values())
        for root_index, comment in enumerate(roots, start=1):
            self._checkpoint()
            pages = self.child_results.get(comment.id) or []
            has_more = bool(pages[-1].get("has_more")) if pages else bool(comment.raw.get("has_more"))
            if not has_more:
                self.collector.child_api_reached_end[comment.id] = True
                continue
            self.logger.info("正在展开：%s / %s", root_index, len(roots))
            idle = 0
            while has_more:
                self._checkpoint()
                before_pages = len(self.child_results.get(comment.id) or [])
                root_id = json.dumps(comment.id, ensure_ascii=False)
                clicked = self.controller.evaluate_json(
                    "() => { const id=" + root_id + "; "
                    "const root=document.querySelector('.link-comment__comment-item[data-comment-id=\"'+CSS.escape(id)+'\"]'); "
                    "if(!root) return false; "
                    "const node=[...root.querySelectorAll('.comment-children__load-all')].reverse().find(n=>n.offsetParent); "
                    "if(!node) return false; node.scrollIntoView({block:'center'}); node.click(); return true; }"
                )
                if not clicked:
                    self.collector.mark_fetch_error(f"child:{comment.id}:button-not-found")
                    break
                self._wait_network_stable(capture_dir)
                pages = self.child_results.get(comment.id) or []
                received = len(pages) > before_pages
                has_more = bool(pages[-1].get("has_more")) if pages else has_more
                idle = 0 if received else idle + 1
                if idle >= 3:
                    self.collector.mark_fetch_error(f"child:{comment.id}:no-response")
                    break
            self.collector.child_api_reached_end[comment.id] = not has_more

    def _expand_long_text(self) -> None:
        self._checkpoint()
        self.controller.evaluate(
            "() => { const labels=['全文','展开全文','展开更多']; let count=0; "
            "for(const n of document.querySelectorAll('button,a,div')) { const t=(n.textContent||'').trim(); "
            "if(n.offsetParent && labels.includes(t)) { n.click(); count++; } } return count; }"
        )

    def _wait_network_stable(self, capture_dir: Path, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        stable = 0
        previous = -1
        while time.monotonic() < deadline:
            self._checkpoint()
            self._drain_network(capture_dir)
            current = len(self.seen_request_ids)
            stable = stable + 1 if current == previous else 0
            if stable >= 3 and time.monotonic() - started >= 2.5:
                return
            previous = current
            time.sleep(0.5)

    def _drain_network(self, capture_dir: Path) -> None:
        if self.control.is_blocked:
            return
        try:
            requests = self.controller.relevant_requests()
        except Exception as error:
            if self.reconnect_attempted:
                raise McpCollectionError("浏览器连接已断开，请点击“重新连接”。") from error
            self.reconnect_attempted = True
            self.logger.info("浏览器连接中断，正在尝试一次重新连接")
            try:
                self.controller.close()
                self.controller.connect()
                pages = self.controller.list_pages()
                target = next(
                    (page for page in pages if self.parsed and self._page_matches(page, self.parsed.link_id)),
                    None,
                )
                if target is None:
                    raise McpCollectionError("帖子页面已被关闭，导出已暂停。请重新打开帖子。")
                self.controller.select_page(target.page_id, bring_to_front=False)
                requests = self.controller.relevant_requests()
            except McpCollectionError:
                raise
            except Exception as reconnect_error:
                raise McpCollectionError("浏览器连接已断开，请点击“重新连接”。") from reconnect_error
        relevant = [item for item in requests if classify_api_url(item.url)]
        for request in relevant:
            if request.request_id in self.seen_request_ids:
                continue
            self.seen_request_ids.add(request.request_id)
            info = classify_api_url(request.url)
            key = request_key_for_url(request.url)
            if info is None or key is None or key in self.seen_request_keys:
                continue
            if request.status == "429":
                self.control.transition(RequestState.RATE_LIMITED, "HTTP 429：你的操作过于频繁，请稍后再试")
                return
            target = capture_dir / f"response-{request.request_id}.json"
            try:
                saved = self.controller.save_response_body(request.request_id, target)
                payload = json.loads(saved.read_text(encoding="utf-8-sig"))
            except Exception as error:
                self.logger.warning("无法读取网络响应 reqid=%s：%s", request.request_id, error)
                continue
            if not isinstance(payload, dict):
                continue
            self.seen_request_keys.add(key)
            self.control.audit.record(request.url, "page_native")
            status = str(payload.get("status") or "")
            message = str(payload.get("msg") or status)
            limited = rate_limit_message(request.status, status, message, payload)
            if limited:
                self.control.transition(RequestState.RATE_LIMITED, limited)
                return
            if status == "show_captcha":
                self.control.transition(RequestState.CAPTCHA_REQUIRED, message)
                return
            if status != "ok":
                self.logger.warning("小黑盒接口提示：%s", message)
                continue
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if self.debug:
                self.raw_payloads.append({"url": request.url, "payload": payload})
            if info.path == LINK_TREE_PATH:
                page_number = info.page or (max(self.top_results, default=0) + 1)
                self.top_results[page_number] = result
                if self.collector:
                    new, replies = self.collector.merge_page(
                        result, page=page_number, is_last=not bool(result.get("has_more_floors"))
                    )
                    self.logger.info("评论页 %s：新增一级评论 %s，新增回复 %s", page_number, new, replies)
            elif info.path == SUB_COMMENTS_PATH:
                root_id = info.root_comment_id
                pages = self.child_results.setdefault(root_id, [])
                pages.append(result)
                if self.collector:
                    self.collector.merge_child_page(
                        root_id, result, lastval=info.lastval, request_index=len(pages)
                    )

    def _checkpoint(self, *, read_page: bool = True) -> None:
        if self.control.is_cancelled:
            raise McpCollectionError("抓取已由用户停止；已停止后续浏览器动作。")
        if self.control.is_blocked:
            if not self.interactive:
                raise InteractionRequiredError(f"{self.control.state.value}：程序已停止全部自动操作。")
            action = self.control.wait_for_user_action()
            if action == UserAction.CANCEL:
                raise McpCollectionError("抓取已由用户停止。")
            expected = (
                action == UserAction.CAPTCHA_COMPLETED and self.control.state == RequestState.CAPTCHA_REQUIRED
            ) or (action == UserAction.RETRY and self.control.state == RequestState.RATE_LIMITED)
            if not expected:
                return self._checkpoint(read_page=read_page)
            snapshot = self.controller.get_snapshot()
            if any(marker in snapshot for marker in RATE_LIMIT_MARKERS):
                self.control.transition(RequestState.RATE_LIMITED, rate_limit_message(snapshot))
                return self._checkpoint(read_page=False)
            if any(marker in snapshot for marker in CAPTCHA_MARKERS):
                self.control.transition(RequestState.CAPTCHA_REQUIRED, "页面仍要求安全验证")
                return self._checkpoint(read_page=False)
            self.control.transition(RequestState.RUNNING, "")
            return
        if not read_page:
            return
        snapshot = self.controller.get_snapshot()
        if any(marker in snapshot for marker in RATE_LIMIT_MARKERS):
            self.control.transition(RequestState.RATE_LIMITED, rate_limit_message(snapshot))
            return self._checkpoint(read_page=False)
        if any(marker in snapshot for marker in CAPTCHA_MARKERS):
            self.control.transition(RequestState.CAPTCHA_REQUIRED, "小黑盒要求进行安全验证")
            return self._checkpoint(read_page=False)
