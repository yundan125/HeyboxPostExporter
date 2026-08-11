from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
import json
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .mcp_client import McpClientError, StdioMcpClient, resolve_node_and_npx


HEYBOX_HOST_MARKERS = ("xiaoheihe.cn", "heybox.hk")
MCP_VERSION = "1.6.0"


@dataclass(frozen=True)
class EdgeEnvironment:
    running: bool
    user_data_dir: Path
    devtools_active_port_path: Path
    devtools_active_port_detected: bool


@dataclass(frozen=True)
class BrowserPage:
    page_id: int
    title: str
    url: str
    selected: bool = False


@dataclass(frozen=True)
class NetworkRequest:
    request_id: int
    method: str
    url: str
    status: str


def normal_edge_user_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "Microsoft" / "Edge" / "User Data").resolve()
    return (Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data").resolve()


def find_edge_executable(configured_path: Path | str | None = None) -> Path | None:
    if configured_path:
        configured = Path(configured_path).expanduser()
        if configured.is_file() and configured.name.casefold() == "msedge.exe":
            return configured.resolve()
    candidates = []
    for name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(name)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return next((item.resolve() for item in candidates if item.is_file()), None)


def _edge_is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    for row in csv.reader(result.stdout.splitlines()):
        if row and row[0].casefold() == "msedge.exe":
            return True
    return False


def _valid_devtools_active_port(path: Path) -> bool:
    try:
        lines = [line.strip() for line in path.read_text(encoding="ascii").splitlines() if line.strip()]
        port = int(lines[0])
    except (OSError, UnicodeError, ValueError, IndexError):
        return False
    return 1 <= port <= 65535 and len(lines) >= 2 and lines[1].startswith("/devtools/browser/")


def inspect_edge_environment(user_data_dir: Path | None = None) -> EdgeEnvironment:
    data_dir = (user_data_dir or normal_edge_user_data_dir()).resolve()
    active_port = data_dir / "DevToolsActivePort"
    return EdgeEnvironment(
        running=_edge_is_running(),
        user_data_dir=data_dir,
        devtools_active_port_path=active_port,
        devtools_active_port_detected=_valid_devtools_active_port(active_port),
    )


class BrowserController(ABC):
    """Browser operations exposed to the exporter without Playwright objects."""

    @abstractmethod
    def list_pages(self, *, on_authorization_waiting: Callable[[], None] | None = None) -> list[BrowserPage]: ...

    @abstractmethod
    def select_page(self, page_id: int, *, bring_to_front: bool = False) -> None: ...

    @abstractmethod
    def create_page(self, url: str, *, background: bool = False, timeout_ms: int = 60_000) -> None: ...

    @abstractmethod
    def navigate(self, url: str, *, timeout_ms: int = 30_000) -> dict[str, Any]: ...

    @abstractmethod
    def click(self, uid: str) -> dict[str, Any]: ...

    @abstractmethod
    def evaluate(self, function: str, args: list[Any] | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def wait(self, texts: list[str], *, timeout_ms: int = 30_000) -> dict[str, Any]: ...

    @abstractmethod
    def get_network_requests(self) -> dict[str, Any]: ...

    @abstractmethod
    def get_response_body(self, request_id: int) -> dict[str, Any]: ...

    @abstractmethod
    def get_page_html(self) -> str: ...

    @abstractmethod
    def get_snapshot(self) -> str: ...

    @abstractmethod
    def close(self) -> None: ...


class ChromeDevToolsMcpController(BrowserController):
    def __init__(self, *, user_data_dir: Path, working_dir: Path, logger: logging.Logger) -> None:
        self.user_data_dir = user_data_dir.resolve()
        self.working_dir = working_dir.resolve()
        self.logger = logger
        self.client: StdioMcpClient | None = None
        self.node_version = ""
        self.mcp_version = ""
        self.selected_page_id: int | None = None
        self.work_page_id: int | None = None

    @property
    def is_available(self) -> bool:
        return self.client is not None and self.client.is_initialized

    @property
    def process_id(self) -> int | None:
        return self.client.pid if self.client is not None else None

    def connect(self) -> None:
        if self.is_available:
            self.logger.info("Browser sidecar: reusing existing initialized MCP process PID %s", self.process_id)
            return
        if self.client is not None:
            self.client.close()
            self.client = None
        node, npx, self.node_version = resolve_node_and_npx()
        args = [
            "-y",
            f"chrome-devtools-mcp@{MCP_VERSION}",
            "--autoConnect",
            f"--userDataDir={self.user_data_dir}",
        ]
        npx_cli = Path(node).resolve().parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
        if os.name == "nt" and npx_cli.is_file():
            # Calling npx.cmd through cmd /c is fragile when Node is installed
            # below "Program Files". This is exactly the same system npx entry
            # point without the batch-file quoting layer.
            command = [node, str(npx_cli), *args]
        elif os.name == "nt" and npx.casefold().endswith((".cmd", ".bat")):
            command_text = subprocess.list2cmdline([npx, *args])
            command = ["cmd.exe", "/d", "/c", f'"{command_text}"']
        else:
            command = [npx, *args]
        self.logger.info("Node version: %s", self.node_version)
        self.logger.info("User Data dir: %s", self.user_data_dir)
        self.client = StdioMcpClient(
            command,
            cwd=self.working_dir,
            logger=self.logger,
            environment={"NO_COLOR": "1"},
        )
        self.client.start()
        self.logger.info("MCP process PID: %s", self.client.pid)
        initialized = self.client.initialize(timeout=90.0)
        server_info = initialized.get("serverInfo") if isinstance(initialized, dict) else None
        if isinstance(server_info, dict):
            self.mcp_version = str(server_info.get("version") or "unknown")
        else:
            self.mcp_version = "unknown"
        self.logger.info("MCP version: %s", self.mcp_version)
        if "list_pages" not in self.client.tools:
            raise McpClientError("chrome-devtools-mcp 未提供 list_pages。")

    def list_pages(self, *, on_authorization_waiting: Callable[[], None] | None = None) -> list[BrowserPage]:
        self.connect()
        assert self.client is not None

        result = self.client.call_tool(
            "list_pages",
            {},
            timeout=600.0,
            waiting_after=5.0,
            on_waiting=on_authorization_waiting,
        )
        self.logger.info("Puppeteer connected")
        pages = parse_list_pages(self.client.extract_text(result))
        selected = next((page for page in pages if page.selected), None)
        self.selected_page_id = selected.page_id if selected else self.selected_page_id
        self.logger.info("list_pages result: SUCCESS")
        self.logger.info("Page count: %s", len(pages))
        return pages

    def select_page(self, page_id: int, *, bring_to_front: bool = False) -> None:
        self._call("select_page", {"pageId": page_id, "bringToFront": bring_to_front})
        self.selected_page_id = page_id

    def create_page(self, url: str, *, background: bool = False, timeout_ms: int = 60_000) -> None:
        self._call("new_page", {"url": url, "background": background, "timeout": timeout_ms})
        pages = self.list_pages()
        selected = next((page for page in pages if page.selected), None)
        if selected:
            self.selected_page_id = selected.page_id
            self.work_page_id = selected.page_id

    def navigate(self, url: str, *, timeout_ms: int = 30_000) -> dict[str, Any]:
        return self._call("navigate_page", {"type": "url", "url": url, "timeout": timeout_ms})

    def click(self, uid: str) -> dict[str, Any]:
        return self._call("click", {"uid": uid})

    def evaluate(self, function: str, args: list[Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"function": function}
        if args is not None:
            payload["args"] = args
        return self._call("evaluate_script", payload)

    def wait(self, texts: list[str], *, timeout_ms: int = 30_000) -> dict[str, Any]:
        return self._call("wait_for", {"text": texts, "timeout": timeout_ms})

    def get_network_requests(self) -> dict[str, Any]:
        return self._call("list_network_requests", {})

    def get_response_body(self, request_id: int) -> dict[str, Any]:
        return self._call("get_network_request", {"reqid": request_id})

    def save_response_body(self, request_id: int, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self._call("get_network_request", {"reqid": request_id, "responseFilePath": str(path)})
        text = self._text(result)
        match = re.search(r"Saved to (.+?)\.?$", text, re.MULTILINE)
        saved = Path(match.group(1).strip()) if match else path
        if not saved.is_absolute():
            saved = self.working_dir / saved
        if saved.is_file():
            return saved
        candidates = [path, Path(str(path) + ".network-response")]
        found = next((item for item in candidates if item.is_file()), None)
        if found is None:
            raise McpClientError(f"无法读取网络响应体：reqid={request_id}")
        return found

    def get_snapshot(self) -> str:
        return self._text(self._call("take_snapshot", {}))

    def get_page_html(self) -> str:
        with tempfile.TemporaryDirectory(prefix="heybox-html-", dir=self.working_dir) as folder:
            path = Path(folder) / "page.json"
            relative = path.relative_to(self.working_dir)
            self._call(
                "evaluate_script",
                {"function": "() => document.documentElement.outerHTML", "filePath": str(relative)},
            )
            candidates = [path, Path(str(path) + ".json")]
            saved = next((item for item in candidates if item.is_file()), None)
            if saved is None:
                raise McpClientError("无法保存当前页面 HTML。")
            value = json.loads(saved.read_text(encoding="utf-8-sig"))
            return str(value or "")

    def get_page_url(self) -> str:
        value = self.evaluate_json("() => location.href")
        return str(value or "")

    def get_page_title(self) -> str:
        value = self.evaluate_json("() => document.title")
        return str(value or "")

    def scroll(self) -> dict[str, Any]:
        return self.evaluate(
            "() => { window.scrollTo(0, document.documentElement.scrollHeight); "
            "return {height: document.documentElement.scrollHeight, y: window.scrollY}; }"
        )

    def evaluate_json(self, function: str) -> Any:
        text = self._text(self.evaluate(function))
        fenced = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else text.rsplit("returned:", 1)[-1].strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return candidate.strip().strip('"')

    def relevant_requests(self) -> list[NetworkRequest]:
        result = self._call(
            "list_network_requests",
            {"resourceTypes": ["xhr", "fetch"], "includePreservedRequests": True, "pageSize": 1000},
        )
        return parse_network_requests(self._text(result))

    def close(self) -> None:
        if self.client is None:
            return
        pid = self.client.pid
        self.client.close()
        self.logger.info("MCP sidecar stopped: PID %s; Edge was not closed", pid)
        self.client = None

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.is_available:
            self.connect()
        assert self.client is not None
        return self.client.call_tool(name, arguments)

    def _text(self, result: dict[str, Any]) -> str:
        assert self.client is not None
        return self.client.extract_text(result)


_PAGE_LINE = re.compile(r"^(\d+):\s*(.*)$")


def parse_list_pages(text: str) -> list[BrowserPage]:
    pages: list[BrowserPage] = []
    for raw_line in text.splitlines():
        match = _PAGE_LINE.match(raw_line.strip())
        if match is None:
            continue
        page_id = int(match.group(1))
        body = match.group(2).strip()
        selected = body.endswith(" [selected]")
        if selected:
            body = body[: -len(" [selected]")].rstrip()
        title = ""
        url = body
        url_match = re.match(r"^(.*) \(([^()]*(?:://|about:|edge:)[^()]*)\)$", body)
        if url_match:
            title = url_match.group(1).strip()
            url = url_match.group(2).strip()
        pages.append(BrowserPage(page_id=page_id, title=title, url=url, selected=selected))
    return pages


_NETWORK_LINE = re.compile(r"^reqid=(\d+)\s+(\S+)\s+(.+)\s+\[([^\]]+)\](?:\s+\[.*\])?$")


def parse_network_requests(text: str) -> list[NetworkRequest]:
    requests: list[NetworkRequest] = []
    for raw_line in text.splitlines():
        match = _NETWORK_LINE.match(raw_line.strip())
        if not match:
            continue
        requests.append(NetworkRequest(int(match.group(1)), match.group(2), match.group(3), match.group(4)))
    return requests
