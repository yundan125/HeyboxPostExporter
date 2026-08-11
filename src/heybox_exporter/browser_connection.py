from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

try:
    import winreg
except ImportError:  # pragma: no cover - only used on Windows
    winreg = None  # type: ignore[assignment]

from playwright.sync_api import Browser, BrowserContext, Page, Playwright


HEYBOX_HOME = "https://www.xiaoheihe.cn/"
AUTH_COOKIE_HINTS = (
    "pkey",
    "heybox_id",
    "heyboxid",
    "userid",
    "user_id",
    "access_token",
)
WORK_PAGE_MARKER = "heybox-post-exporter-work-tab"
WORK_PAGE_URL = f"about:blank#{WORK_PAGE_MARKER}"
EDGE_DEBUG_HOST = "127.0.0.1"
EDGE_DEBUG_PORT = 9222
EDGE_RESTART_MESSAGE = (
    "未检测到当前 Microsoft Edge 的可用远程调试连接。\n\n"
    "请在 Edge 中打开 edge://inspect/#remote-debugging，勾选 “Allow remote debugging for this browser instance”。\n\n"
    "如果页面一直显示 Server running at: starting…，请先取消勾选，等待 2 秒后重新勾选。"
    "程序不会关闭或重新启动你的 Edge。"
)


class BrowserMode(str, Enum):
    """The exporter uses the installed Microsoft Edge with its normal profile."""

    EDGE = "edge"


class BrowserConnectionError(RuntimeError):
    def __init__(self, message: str, *, status_text: str = "连接失败") -> None:
        super().__init__(message)
        self.status_text = status_text


class EdgeRestartRequired(BrowserConnectionError):
    """Raised when normal Edge is running without a usable startup CDP endpoint."""


StatusCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class CdpEndpoint:
    user_data_dir: Path
    endpoint_url: str
    host: str
    port: int
    source: str
    browser_version: str = ""

    @property
    def safe_label(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class ContextStatus:
    index: int
    page_count: int
    heybox_page_count: int
    logged_in: bool
    current_user: str = ""


@dataclass
class EdgeSession:
    browser: Browser
    context: BrowserContext
    page: Page
    endpoint: CdpEndpoint
    context_status: ContextStatus
    edge_executable: Path
    user_data_dir: Path
    profile_path: Path
    profile_name: str
    profile_source: str
    existing_cdp_detected: bool
    started_by_app: bool
    existing_contexts: int
    existing_pages: int
    edge_policy: "EdgePolicyStatus | None" = None


@dataclass(frozen=True)
class EdgePolicyStatus:
    """The registry state which controls Edge's remote debugging policy.

    ``configured_state`` deliberately preserves the raw policy distinction the
    user needs when diagnosing a machine: ``not_configured``, ``0``, ``1`` or
    ``other``.  ``effective`` is the resulting behavior used by the launcher.
    """

    machine_value: int | None
    user_value: int | None
    configured_state: str
    effective: str
    effective_value: int | None
    source: str

    @property
    def summary(self) -> str:
        machine = "未配置" if self.machine_value is None else str(self.machine_value)
        user = "未配置" if self.user_value is None else str(self.user_value)
        return (
            f"HKLM={machine}; HKCU={user}; "
            f"RemoteDebuggingAllowed={self.configured_state}; effective={self.effective}"
        )


@dataclass(frozen=True)
class EndpointProbe:
    host: str
    path: str
    port: int
    status: int | None
    content_type: str
    content_length: int | None
    payload: object | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and not self.error

    @property
    def label(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"


@dataclass(frozen=True)
class TcpListener:
    local_address: str
    local_port: int
    owning_pid: int
    process_name: str = ""


def edge_executable_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return tuple(candidates)


def find_edge_executable(configured_path: Path | str | None = None) -> Path | None:
    if configured_path:
        configured = Path(configured_path).expanduser()
        if configured.is_file() and configured.name.lower() == "msedge.exe":
            return configured.resolve()
    return next((path.resolve() for path in edge_executable_candidates() if path.is_file()), None)


def normal_edge_user_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "Microsoft" / "Edge" / "User Data").resolve()
    return (Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data").resolve()


def edge_launch_command(executable: Path) -> tuple[str, ...]:
    """Return a normal Edge command without automation or debugging switches."""

    return (str(executable),)


def parse_devtools_active_port(path: Path, user_data_dir: Path | None = None) -> CdpEndpoint | None:
    try:
        lines = [line.strip() for line in path.read_text(encoding="ascii").splitlines() if line.strip()]
        port = int(lines[0])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    if not 1 <= port <= 65535:
        return None
    websocket = lines[1] if len(lines) > 1 else ""
    if websocket.startswith("/"):
        websocket = f"ws://127.0.0.1:{port}{websocket}"
    elif not websocket.startswith(("ws://", "wss://")):
        websocket = f"http://127.0.0.1:{port}"
    data_dir = (user_data_dir or path.parent).resolve()
    return CdpEndpoint(
        user_data_dir=data_dir,
        endpoint_url=websocket,
        host="127.0.0.1",
        port=port,
        source=f"DevToolsActivePort ({data_dir})",
    )


def _read_remote_debugging_policy_value(root: object, subkey: str) -> int | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(root, subkey) as key:  # type: ignore[arg-type]
            value, _value_type = winreg.QueryValueEx(key, "RemoteDebuggingAllowed")
    except (FileNotFoundError, OSError, TypeError):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_edge_debug_policy() -> EdgePolicyStatus:
    """Read HKLM/HKCU policy without changing either registry hive."""

    if winreg is None:
        return EdgePolicyStatus(None, None, "not_configured", "unknown", None, "unsupported")
    subkey = r"SOFTWARE\Policies\Microsoft\Edge"
    machine_value = _read_remote_debugging_policy_value(winreg.HKEY_LOCAL_MACHINE, subkey)
    user_value = _read_remote_debugging_policy_value(winreg.HKEY_CURRENT_USER, subkey)
    if machine_value is None and user_value is None:
        return EdgePolicyStatus(None, None, "not_configured", "unknown", None, "none")

    # Machine policy takes precedence over user policy when both are present.
    if machine_value is not None:
        effective_value = machine_value
        source = "HKLM"
    else:
        effective_value = user_value
        source = "HKCU"
    if effective_value == 0:
        effective = "disabled"
        configured_state = "0"
    elif effective_value == 1:
        effective = "allowed"
        configured_state = "1"
    else:
        effective = "unknown"
        configured_state = "other"
    return EdgePolicyStatus(
        machine_value=machine_value,
        user_value=user_value,
        configured_state=configured_state,
        effective=effective,
        effective_value=effective_value,
        source=source,
    )


def _probe_json_endpoint(host: str, path: str, port: int = EDGE_DEBUG_PORT, timeout: float = 0.8) -> EndpointProbe:
    """Probe one local DevTools HTTP endpoint without using environment proxies."""

    opener = build_opener(ProxyHandler({}))
    request = Request(
        f"http://{host}:{port}{path}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            status = int(response.status)
            content_type = str(response.headers.get("Content-Type") or "")
            payload: object | None = None
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, ValueError, json.JSONDecodeError):
                payload = None
            return EndpointProbe(
                host=host,
                path=path,
                port=port,
                status=status,
                content_type=content_type,
                content_length=len(body),
                payload=payload,
            )
    except HTTPError as error:
        content_type = str(error.headers.get("Content-Type") or "") if error.headers else ""
        return EndpointProbe(
            host=host,
            path=path,
            port=port,
            status=int(error.code),
            content_type=content_type,
            content_length=None,
            payload=None,
            error=f"HTTPError {error.code}: {error.reason}",
        )
    except (OSError, URLError, TimeoutError) as error:
        return EndpointProbe(
            host=host,
            path=path,
            port=port,
            status=None,
            content_type="",
            content_length=None,
            payload=None,
            error=f"{type(error).__name__}: {error}",
        )


def probe_edge_cdp(port: int = EDGE_DEBUG_PORT) -> tuple[EndpointProbe, ...]:
    """Probe both local host spellings and both standard DevTools endpoints."""

    return tuple(
        _probe_json_endpoint(host, path, port=port, timeout=0.8)
        for host in ("127.0.0.1", "localhost")
        for path in ("/json/version", "/json/list")
    )


def _probe_version(port: int, timeout: float = 0.8) -> dict[str, str] | None:
    probe = _probe_json_endpoint("127.0.0.1", "/json/version", port=port, timeout=timeout)
    payload = probe.payload
    if not isinstance(payload, dict):
        return None
    return {str(key): str(value) for key, value in payload.items() if value is not None}


def _is_edge(payload: dict[str, str]) -> bool:
    product = " ".join((payload.get("Browser", ""), payload.get("User-Agent", ""))).lower()
    return "edg/" in product or "microsoft edge" in product


def _is_valid_cdp_payload(payload: dict[str, str] | None) -> bool:
    if not payload or not _is_edge(payload):
        return False
    return bool(
        payload.get("Browser")
        and payload.get("Protocol-Version")
        and payload.get("webSocketDebuggerUrl", "").startswith(("ws://", "wss://"))
    )


def valid_edge_endpoint(user_data_dir: Path | None = None) -> CdpEndpoint | None:
    data_dir = (user_data_dir or normal_edge_user_data_dir()).resolve()
    candidates: list[CdpEndpoint] = []
    active = parse_devtools_active_port(data_dir / "DevToolsActivePort", data_dir)
    if active is not None:
        candidates.append(active)
    if not any(candidate.port == EDGE_DEBUG_PORT for candidate in candidates):
        candidates.append(
            CdpEndpoint(
                user_data_dir=data_dir,
                endpoint_url=f"http://{EDGE_DEBUG_HOST}:{EDGE_DEBUG_PORT}",
                host=EDGE_DEBUG_HOST,
                port=EDGE_DEBUG_PORT,
                source="兼容端口探测",
            )
        )
    for candidate in candidates:
        payload = _probe_version(candidate.port)
        if not _is_valid_cdp_payload(payload):
            continue
        return CdpEndpoint(
            user_data_dir=data_dir,
            endpoint_url=payload.get("webSocketDebuggerUrl") or candidate.endpoint_url,
            host=EDGE_DEBUG_HOST,
            port=candidate.port,
            source=candidate.source,
            browser_version=payload.get("Browser", ""),
        )
    return None


def discover_edge_endpoint(user_data_dir: Path | None = None) -> CdpEndpoint | None:
    """Discover the current Edge instance from DevToolsActivePort, with 9222 fallback."""

    return valid_edge_endpoint(user_data_dir)


def _edge_process_command_lines() -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            startupinfo=startup_info,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        payload = json.loads(result.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    rows = payload if isinstance(payload, list) else [payload]
    output: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        command_line = str(row.get("CommandLine") or "")
        try:
            process_id = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if process_id:
            output.append((process_id, command_line))
    return output


def edge_process_ids() -> list[int]:
    """Return every msedge.exe PID, never including msedgewebview2.exe."""

    return [process_id for process_id, _command_line in _edge_process_command_lines()]


def edge_browser_process_ids() -> list[int]:
    """Return Edge browser-process PIDs, excluding renderer/GPU/utility children."""

    result: list[int] = []
    for process_id, command_line in _edge_process_command_lines():
        normalized = command_line.replace('"', "").casefold()
        if "--type=" not in normalized and "--type " not in normalized:
            result.append(process_id)
    return result


def _edge_browser_has_debug_argument(process_ids: set[int] | None = None) -> bool:
    for process_id, command_line in _edge_process_command_lines():
        if process_ids is not None and process_id not in process_ids:
            continue
        normalized = command_line.replace('"', "").casefold()
        if "--type=" in normalized or "--type " in normalized:
            continue
        if re.search(r"--remote-debugging-port(?:=|\s+)9222(?:\s|$)", normalized):
            return True
    return False


def _port_is_listening(host: str = EDGE_DEBUG_HOST, port: int = EDGE_DEBUG_PORT) -> bool:
    hosts = (host,) if host else ("127.0.0.1", "localhost")
    for candidate in hosts:
        try:
            with socket.create_connection((candidate, port), timeout=0.4):
                return True
        except OSError:
            continue
    return False


def _tcp_listener_details(port: int = EDGE_DEBUG_PORT) -> list[TcpListener]:
    """Return local listeners with address, port, PID and process name."""

    if os.name != "nt":
        return []
    script = (
        f"@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
        "ForEach-Object { $process = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; "
        "[PSCustomObject]@{ LocalAddress=$_.LocalAddress; LocalPort=$_.LocalPort; "
        "OwningProcess=$_.OwningProcess; ProcessName=if ($process) {$process.ProcessName} else {''} } }) | "
        "ConvertTo-Json -Compress"
    )
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            startupinfo=startup_info,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        payload = json.loads(result.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    rows = payload if isinstance(payload, list) else [payload]
    listeners: list[TcpListener] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            local_port = int(row.get("LocalPort") or port)
            owning_pid = int(row.get("OwningProcess") or 0)
        except (TypeError, ValueError):
            continue
        if not owning_pid:
            continue
        listeners.append(
            TcpListener(
                local_address=str(row.get("LocalAddress") or ""),
                local_port=local_port,
                owning_pid=owning_pid,
                process_name=str(row.get("ProcessName") or ""),
            )
        )
    return listeners


def _tcp_listener_owner_ids(port: int = EDGE_DEBUG_PORT) -> set[int]:
    return {listener.owning_pid for listener in _tcp_listener_details(port)}


def _format_process_snapshot(snapshot: list[tuple[int, str]] | None) -> list[str]:
    if not snapshot:
        return ["<none>"]
    return [f"PID {process_id}: {command_line or '<command line unavailable>'}" for process_id, command_line in snapshot]


def _edge_version_label(executable: Path | None, snapshot: list[tuple[int, str]] | None) -> str:
    for _process_id, command_line in snapshot or []:
        match = re.search(r"--annotation=ver=([^\s]+)", command_line)
        if match:
            return match.group(1)
    if executable is None or os.name != "nt":
        return "<unknown>"
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-Item -LiteralPath $args[0]).VersionInfo.ProductVersion",
                str(executable),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            startupinfo=startup_info,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "<unknown>"
    return (result.stdout or "").strip() or "<unknown>"


def write_edge_cdp_diagnostic(
    *,
    executable: Path | None = None,
    user_data_dir: Path | None = None,
    debug_dir: Path | None = None,
    process_ids_before: list[tuple[int, str]] | None = None,
    process_ids_after: list[tuple[int, str]] | None = None,
    listeners_before: list[TcpListener] | None = None,
    listeners_after: list[TcpListener] | None = None,
    popen_pid: int | None = None,
    stage: str = "",
    error: str = "",
) -> Path:
    """Write a local-only Edge/CDP snapshot, never including cookies or tokens."""

    root = (debug_dir or (Path.cwd() / "debug")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "edge-cdp-diagnostic.txt"
    policy = read_edge_debug_policy()
    listeners = listeners_after if listeners_after is not None else _tcp_listener_details(EDGE_DEBUG_PORT)
    probes = probe_edge_cdp(EDGE_DEBUG_PORT)
    current_snapshot = process_ids_after or _edge_process_command_lines()
    profile_path, profile_source = _last_used_profile_path(user_data_dir or normal_edge_user_data_dir())
    before_listening = bool(listeners_before) if listeners_before is not None else None
    after_listening = bool(listeners)
    lines = [
        "Heybox Exporter Edge CDP diagnostic",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"Stage: {stage or '<unspecified>'}",
        f"Error: {error or '<none>'}",
        f"Edge executable: {executable or '<unknown>'}",
        f"Edge version: {_edge_version_label(executable, current_snapshot)}",
        f"Edge User Data: {user_data_dir or normal_edge_user_data_dir()}",
        f"Edge profile path ({profile_source}): {profile_path}",
        f"Popen PID: {popen_pid if popen_pid is not None else '<none>'}",
        "",
        "[RemoteDebuggingAllowed policy]",
        f"HKLM value: {policy.machine_value if policy.machine_value is not None else 'not configured'}",
        f"HKCU value: {policy.user_value if policy.user_value is not None else 'not configured'}",
        f"Configured state: {policy.configured_state}",
        f"Effective: {policy.effective} (source={policy.source})",
        "",
        "[msedge.exe before]",
        *_format_process_snapshot(process_ids_before),
        "",
        "[msedge.exe after/current]",
        *_format_process_snapshot(current_snapshot),
        "",
        "[9222 TCP listeners]",
        f"Port listening before Popen: {'yes' if before_listening else 'no' if before_listening is not None else '<not captured>'}",
        f"Port listening after/current: {'yes' if after_listening else 'no'}",
    ]
    if listeners:
        lines.extend(
            f"{listener.local_address}:{listener.local_port} PID {listener.owning_pid} "
            f"{listener.process_name or '<unknown>'}"
            for listener in listeners
        )
    else:
        lines.append("<none>")
    lines.extend(["", "[JSON endpoint probes]"])
    for probe in probes:
        lines.append(
            f"{probe.label}: status={probe.status if probe.status is not None else '<none>'}; "
            f"content-type={probe.content_type or '<none>'}; "
            f"length={probe.content_length if probe.content_length is not None else '<none>'}; "
            f"error={probe.error or '<none>'}; json={'yes' if probe.payload is not None else 'no'}"
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def _emit_status(callback: StatusCallback | None, status: str, guidance: str) -> None:
    if callback is not None:
        callback(status, guidance)


def _wait_for_endpoint(user_data_dir: Path, timeout_seconds: float) -> CdpEndpoint | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        endpoint = discover_edge_endpoint(user_data_dir)
        if endpoint is not None:
            return endpoint
        time.sleep(0.4)
    return None


def request_normal_edge_exit(process_ids: list[int] | None = None) -> bool:
    """Disabled: the exporter never closes a user-owned Edge instance."""

    return False


def _wait_for_normal_edge_exit(logger: logging.Logger | None, timeout_seconds: float = 9.0) -> bool:
    """Poll all msedge.exe processes; log required 1/3/5 second checkpoints."""

    started = time.monotonic()
    checkpoints = (1.0, 3.0, 5.0)
    logged: set[float] = set()
    while time.monotonic() - started < timeout_seconds:
        elapsed = time.monotonic() - started
        process_count = len(edge_process_ids())
        for checkpoint in checkpoints:
            if checkpoint not in logged and elapsed >= checkpoint:
                if logger is not None:
                    logger.info("Running Edge processes after %ss: %s", int(checkpoint), process_count)
                logged.add(checkpoint)
        if elapsed >= checkpoints[-1] and process_count == 0:
            return True
        time.sleep(0.25)
    return not edge_process_ids()


def _force_terminate_edge_processes(
    logger: logging.Logger | None,
    timeout_seconds: float = 6.0,
    target_snapshot: set[int] | None = None,
) -> bool:
    """Disabled: the exporter never terminates user-owned Edge processes."""

    return False


def _shutdown_edge_for_restart(
    executable: Path,
    logger: logging.Logger | None,
    status_callback: StatusCallback | None,
) -> None:
    """Disabled: restarting the user's Edge is outside exporter scope."""

    raise BrowserConnectionError(
        "程序不会关闭或重新启动你的 Microsoft Edge。",
        status_text="等待 Edge 远程调试",
    )


def _connect(playwright: Playwright, endpoint: CdpEndpoint, logger: logging.Logger | None = None) -> Browser:
    if logger is not None:
        logger.info("Playwright connect_over_cdp: %s", endpoint.endpoint_url)
    try:
        browser = playwright.chromium.connect_over_cdp(
            endpoint.endpoint_url,
            timeout=10_000,
            is_local=True,
            no_defaults=True,
        )
    except Exception as error:
        if logger is not None:
            logger.error("Playwright connect_over_cdp: failed: %s", error)
        raise BrowserConnectionError(
            f"CDP 127.0.0.1:9222 已可用，但 Playwright connect_over_cdp() 失败：{error}",
            status_text="Edge 已启动，但 Playwright 连接失败",
        ) from error
    if logger is not None:
        logger.info("Playwright connect_over_cdp: success")
    return browser


def _is_heybox_page(page: Page) -> bool:
    return "xiaoheihe.cn" in (page.url or "").lower()


def _page_storage_signal(page: Page) -> tuple[bool, str]:
    script = """
    () => {
      const authPattern = /(login|account|user[_-]?info|profile|pkey|session|token)/i;
      const namePattern = /^(username|nickname|nick_name|display_name|name)$/i;
      let auth = false;
      let currentUser = '';
      const inspectStorage = (storage) => {
        for (let i = 0; i < storage.length; i++) {
          const key = storage.key(i) || '';
          if (!authPattern.test(key)) continue;
          auth = true;
          if (currentUser || /token|pkey|session/i.test(key)) continue;
          const raw = storage.getItem(key) || '';
          if (raw.length > 4096) continue;
          try {
            const value = JSON.parse(raw);
            const queue = [value];
            while (queue.length && !currentUser) {
              const item = queue.shift();
              if (!item || typeof item !== 'object') continue;
              for (const [childKey, childValue] of Object.entries(item)) {
                if (namePattern.test(childKey) && typeof childValue === 'string' && childValue.length <= 80) {
                  currentUser = childValue;
                  break;
                }
                if (childValue && typeof childValue === 'object') queue.push(childValue);
              }
            }
          } catch (_) {}
        }
      };
      try { inspectStorage(localStorage); } catch (_) {}
      try { inspectStorage(sessionStorage); } catch (_) {}
      const loginButtons = [...document.querySelectorAll('button,a')]
        .some((node) => (node.textContent || '').trim() === '登录' && node.getClientRects().length);
      return {auth, currentUser, loginButtons};
    }
    """
    try:
        result = page.evaluate(script)
    except Exception:
        return False, ""
    if not isinstance(result, dict):
        return False, ""
    return bool(result.get("auth")) and not bool(result.get("loginButtons")), str(result.get("currentUser") or "")[:80]


def _is_work_page(page: Page) -> bool:
    if (page.url or "") == WORK_PAGE_URL:
        return True
    try:
        return page.evaluate("() => window.name") == WORK_PAGE_MARKER
    except Exception:
        return False


def _find_work_page(context: BrowserContext) -> Page | None:
    return next((page for page in context.pages if _is_work_page(page)), None)


def choose_work_page(context: BrowserContext) -> Page:
    """Return only the exporter-owned tab; never reuse or navigate a user's tab."""

    page = _find_work_page(context)
    if page is not None:
        return page
    page = context.new_page()
    page.goto(WORK_PAGE_URL)
    page.evaluate("marker => { window.name = marker; document.title = 'Heybox Exporter 工作标签页'; }", WORK_PAGE_MARKER)
    return page


def inspect_context(context: BrowserContext, index: int = 0, work_page: Page | None = None) -> ContextStatus:
    pages = list(context.pages)
    heybox_pages = [page for page in pages if _is_heybox_page(page)]
    cookie_names: set[str] = set()
    try:
        cookies = context.cookies([HEYBOX_HOME, "https://api.xiaoheihe.cn/"])
        cookie_names = {str(cookie.get("name") or "").lower() for cookie in cookies}
    except Exception:
        pass
    cookie_auth = any(hint == name or hint in name for name in cookie_names for hint in AUTH_COOKIE_HINTS)
    storage_auth = False
    current_user = ""
    if work_page is not None and _is_heybox_page(work_page):
        storage_auth, current_user = _page_storage_signal(work_page)
    return ContextStatus(
        index=index,
        page_count=len(pages),
        heybox_page_count=len(heybox_pages),
        logged_in=bool(cookie_auth or storage_auth),
        current_user=current_user,
    )


def _last_used_profile_path(user_data_dir: Path) -> tuple[Path, str]:
    try:
        payload = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8"))
        profile = payload.get("profile") if isinstance(payload, dict) else None
        profile_name = str(profile.get("last_used") or "") if isinstance(profile, dict) else ""
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        profile_name = ""
    if not profile_name or profile_name in {".", ".."} or Path(profile_name).name != profile_name:
        profile_name = "Default"
    return (user_data_dir / profile_name).resolve(), "Edge Local State"


def _profile_from_edge_version(context: BrowserContext, user_data_dir: Path, work_page: Page) -> tuple[Path, str]:
    fallback = _last_used_profile_path(user_data_dir)
    try:
        work_page.goto("edge://version/", wait_until="domcontentloaded", timeout=10_000)
        profile_text = (work_page.locator("#profile_path").text_content(timeout=5_000) or "").strip()
        if profile_text:
            profile_path = Path(profile_text).resolve()
            try:
                profile_path.relative_to(user_data_dir)
            except ValueError as error:
                raise BrowserConnectionError(
                    "当前 CDP 连接的 Edge Profile 不在日常 Microsoft Edge User Data 目录内，"
                    "为避免连接到独立/临时 Profile，程序已停止。\n"
                    f"检测到 Profile Path：{profile_path}\n正常 User Data：{user_data_dir}",
                    status_text="Edge Profile 路径不匹配",
                ) from error
            if profile_path.is_dir():
                return profile_path, "edge://version"
    except BrowserConnectionError:
        raise
    except Exception:
        pass
    finally:
        try:
            work_page.goto(WORK_PAGE_URL)
            work_page.evaluate(
                "marker => { window.name = marker; document.title = 'Heybox Exporter 工作标签页'; }",
                WORK_PAGE_MARKER,
            )
        except Exception:
            pass
    return fallback


def _inspect_edge_internal_pages(page: Page, logger: logging.Logger | None) -> None:
    """Record edge://policy and edge://version facts without exposing cookies."""

    if logger is None:
        return
    try:
        page.goto("edge://policy", wait_until="domcontentloaded", timeout=10_000)
        policy_text = (page.locator("body").inner_text(timeout=5_000) or "").strip()
        policy_lines = [
            line.strip()
            for line in policy_text.splitlines()
            if "RemoteDebuggingAllowed" in line or "远程调试" in line
        ]
        logger.info("edge://policy RemoteDebuggingAllowed: %s", " | ".join(policy_lines) or "未在页面文本中找到")
    except Exception as error:
        logger.info("edge://policy inspection unavailable: %s", error)
    try:
        page.goto("edge://version", wait_until="domcontentloaded", timeout=10_000)
        for selector, label in (
            ("#executable_path", "executable path"),
            ("#profile_path", "profile path"),
            ("#command_line", "command line"),
        ):
            try:
                value = (page.locator(selector).text_content(timeout=2_000) or "").strip()
            except Exception:
                value = ""
            if value:
                logger.info("edge://version %s: %s", label, value)
    except Exception as error:
        logger.info("edge://version inspection unavailable: %s", error)
    finally:
        try:
            page.goto(WORK_PAGE_URL)
            page.evaluate(
                "marker => { window.name = marker; document.title = 'Heybox Exporter 工作标签页'; }",
                WORK_PAGE_MARKER,
            )
        except Exception:
            pass


def _select_context_and_work_page(browser: Browser) -> tuple[BrowserContext, Page, int, int]:
    contexts = list(browser.contexts)
    if not contexts:
        raise BrowserConnectionError("已连接正常 Microsoft Edge，但没有可用的 Browser Context。")
    existing_pages = sum(len(context.pages) for context in contexts)
    for index, context in enumerate(contexts):
        page = _find_work_page(context)
        if page is not None:
            return context, page, index, existing_pages
    return contexts[0], choose_work_page(contexts[0]), 0, existing_pages


def _log_lifecycle(logger: logging.Logger | None, session: EdgeSession) -> None:
    if logger is None:
        return
    authenticated_user = session.context_status.current_user or (
        "logged in (identity unavailable)" if session.context_status.logged_in else "not logged in"
    )
    logger.info("Edge mode: normal user profile")
    logger.info("Edge executable: %s", session.edge_executable)
    logger.info("Edge user data dir: %s", session.user_data_dir)
    logger.info("Edge profile: %s", session.profile_name)
    logger.info("Edge profile path: %s", session.profile_path)
    logger.info("Edge profile source: %s", session.profile_source)
    logger.info("Dedicated Profile: disabled; only the normal Edge User Data is used")
    if session.edge_policy is not None:
        logger.info("RemoteDebuggingAllowed policy: %s", session.edge_policy.summary)
    logger.info("Existing CDP detected: %s", "yes" if session.existing_cdp_detected else "no")
    logger.info("DevTools port: %s", session.endpoint.port)
    for probe in probe_edge_cdp(session.endpoint.port):
        logger.info(
            "CDP probe %s: status=%s content-type=%s length=%s error=%s",
            probe.label,
            probe.status if probe.status is not None else "<none>",
            probe.content_type or "<none>",
            probe.content_length if probe.content_length is not None else "<none>",
            probe.error or "<none>",
        )
    logger.info("CDP connected: yes")
    logger.info("Heybox authenticated user: %s", authenticated_user)


def _build_session(
    playwright: Playwright,
    endpoint: CdpEndpoint,
    executable: Path,
    *,
    existing_cdp: bool,
    started_by_app: bool,
    verify_profile: bool,
    force_open_heybox: bool,
    logger: logging.Logger | None,
) -> EdgeSession:
    browser = _connect(playwright, endpoint, logger)
    context, page, context_index, existing_pages = _select_context_and_work_page(browser)
    if verify_profile:
        profile_path, profile_source = _profile_from_edge_version(context, endpoint.user_data_dir, page)
    else:
        profile_path, profile_source = _last_used_profile_path(endpoint.user_data_dir)
    edge_policy = read_edge_debug_policy()
    _inspect_edge_internal_pages(page, logger)
    if force_open_heybox:
        if not _is_heybox_page(page):
            page.goto(HEYBOX_HOME, wait_until="domcontentloaded", timeout=60_000)
        page.bring_to_front()
    status = inspect_context(context, context_index, page)
    session = EdgeSession(
        browser=browser,
        context=context,
        page=page,
        endpoint=endpoint,
        context_status=status,
        edge_executable=executable,
        user_data_dir=endpoint.user_data_dir,
        profile_path=profile_path,
        profile_name=profile_path.name,
        profile_source=profile_source,
        existing_cdp_detected=existing_cdp,
        started_by_app=started_by_app,
        existing_contexts=len(browser.contexts),
        existing_pages=existing_pages,
        edge_policy=edge_policy,
    )
    _log_lifecycle(logger, session)
    return session


def _ensure_edge_debug_policy_allows_launch(
    *,
    executable: Path,
    user_data_dir: Path,
    debug_dir: Path | None,
    logger: logging.Logger | None,
) -> EdgePolicyStatus:
    policy = read_edge_debug_policy()
    if logger is not None:
        logger.info("RemoteDebuggingAllowed policy before launch: %s", policy.summary)
    if policy.effective == "disabled":
        diagnostic = write_edge_cdp_diagnostic(
            executable=executable,
            user_data_dir=user_data_dir,
            debug_dir=debug_dir,
            stage="policy-disabled-before-launch",
            error="RemoteDebuggingAllowed=0",
        )
        raise BrowserConnectionError(
            "Edge 的策略 RemoteDebuggingAllowed=0，已禁用远程调试接口，程序不会修改策略。\n\n"
            "请在 Edge 地址栏打开 edge://policy，确认该策略由谁配置；解除策略后再重试。\n"
            f"已保存诊断：{diagnostic}",
            status_text="Edge 策略已禁用调试接口",
        )
    return policy


def _raise_if_fixed_port_is_unavailable() -> None:
    socket_listening = _port_is_listening(EDGE_DEBUG_HOST) or _port_is_listening("localhost")
    listeners = _tcp_listener_details() if socket_listening else []
    if not socket_listening and not listeners:
        return
    owner_ids = {listener.owning_pid for listener in listeners}
    edge_ids = set(edge_process_ids())
    if owner_ids and owner_ids.isdisjoint(edge_ids):
        details = ", ".join(
            f"{listener.local_address}:{listener.local_port} PID {listener.owning_pid} "
            f"{listener.process_name or '<unknown>'}"
            for listener in listeners
        )
        detail = f"占用进程：{details or sorted(owner_ids)}。"
        status = "9222 端口已被占用"
    elif owner_ids:
        detail = f"当前监听器属于 Edge PID {sorted(owner_ids)}，但没有返回有效的 Edge DevTools 数据。"
        status = "Edge 9222 调试接口无效"
    else:
        detail = "已检测到 9222 正在监听，但暂时无法读取监听器归属。"
        status = "9222 端口状态异常"
    raise BrowserConnectionError(f"9222 端口已被占用或不可用。{detail}", status_text=status)


def _launch_edge_and_wait(
    executable: Path,
    user_data_dir: Path,
    logger: logging.Logger | None,
    status_callback: StatusCallback | None,
    debug_dir: Path | None = None,
) -> CdpEndpoint:
    """Disabled: Attach mode never launches a user browser."""

    raise BrowserConnectionError(
        "程序不会启动新的 Microsoft Edge。请先在当前 Edge 中启用远程调试。",
        status_text="等待 Edge 远程调试",
    )

    _ensure_edge_debug_policy_allows_launch(
        executable=executable,
        user_data_dir=user_data_dir,
        debug_dir=debug_dir,
        logger=logger,
    )
    _raise_if_fixed_port_is_unavailable()
    listeners_before = (
        _tcp_listener_details(EDGE_DEBUG_PORT)
        if (_port_is_listening(EDGE_DEBUG_HOST) or _port_is_listening("localhost"))
        else []
    )
    before_snapshot = _edge_process_command_lines()
    if before_snapshot:
        diagnostic = write_edge_cdp_diagnostic(
            executable=executable,
            user_data_dir=user_data_dir,
            debug_dir=debug_dir,
            process_ids_before=before_snapshot,
            listeners_before=listeners_before,
            stage="before-launch-not-empty",
            error="old msedge.exe processes remained before Popen",
        )
        raise BrowserConnectionError(
            "启动 Edge 前仍检测到旧的 msedge.exe 进程，未启动第二个实例。\n"
            f"请先使用“关闭并重新启动 Edge”完成关闭；诊断：{diagnostic}",
            status_text="Edge 旧进程尚未退出",
        )
    command = edge_launch_command(executable)
    _emit_status(
        status_callback,
        "正在重新启动 Microsoft Edge……",
        "正在使用正常 Edge Profile 重新启动 Microsoft Edge……",
    )
    if logger is not None:
        logger.info("Launching Edge...")
        logger.info("Command:\n%s", subprocess.list2cmdline(list(command)))
    launched_at = time.time()
    try:
        process = subprocess.Popen(command, close_fds=True)
    except (FileNotFoundError, PermissionError, OSError) as error:
        if logger is not None:
            logger.error("Failed to launch Edge")
            logger.error("Executable:\n%s", executable)
            logger.error("Error:\n%s: %s", type(error).__name__, error)
            logger.error("Popen return/error: %s: %s", type(error).__name__, error)
        raise BrowserConnectionError(
            f"Edge 已关闭，但重新启动失败。\n\n{type(error).__name__}: {error}",
            status_text="Edge 已关闭，但重新启动失败",
        ) from error

    if logger is not None:
        logger.info("Popen PID: %s", process.pid)
        logger.info("Popen return/error: running (return code: %s)", process.poll())
        logger.info("New Edge launch: %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(launched_at)))
        logger.info("Launcher Popen PID: %s (actual browser PID will be correlated from msedge.exe)", process.pid)
        logger.info("Waiting for CDP: %s:%s", EDGE_DEBUG_HOST, EDGE_DEBUG_PORT)

    started = time.monotonic()
    deadline = started + 20.0
    checkpoints = (0.5, 1.0, 3.0, 5.0)
    logged: set[float] = set()
    browser_ids: list[int] = []
    process_appeared = False
    endpoint: CdpEndpoint | None = None
    process_status_emitted = False
    port_status_emitted = False
    listener_details_checked = False
    listeners: list[TcpListener] = []
    actual_browser_ids: list[int] = []
    while time.monotonic() < deadline:
        elapsed = time.monotonic() - started
        checkpoint_due = any(checkpoint not in logged and elapsed >= checkpoint for checkpoint in checkpoints)
        current_snapshot = _edge_process_command_lines()
        current_by_id = {process_id: command_line for process_id, command_line in current_snapshot}
        browser_ids = [
            process_id
            for process_id, command_line in current_snapshot
            if "--type=" not in command_line.replace('"', "").casefold()
            and "--type " not in command_line.replace('"', "").casefold()
        ]
        if not process_appeared or checkpoint_due:
            process_appeared = process_appeared or bool(browser_ids)
            if process_appeared:
                old_ids = {process_id for process_id, _command_line in before_snapshot}
                actual_browser_ids = sorted(set(browser_ids) - old_ids) or sorted(set(browser_ids))
        if process_appeared and not process_status_emitted:
            _emit_status(
                status_callback,
                "Edge 主进程已启动，正在检查启动参数……",
                "已检测到 Edge Browser 主进程，正在确认 --remote-debugging-port=9222。",
            )
            process_status_emitted = True
            if logger is not None:
                logger.info("Actual Edge Browser PID(s): %s", actual_browser_ids)
                for process_id in actual_browser_ids:
                    logger.info("Actual Edge Browser command line PID %s: %s", process_id, current_by_id.get(process_id, ""))
        port_listening = _port_is_listening(EDGE_DEBUG_HOST) or _port_is_listening("localhost")
        if port_listening and not listener_details_checked:
            listeners = _tcp_listener_details()
            listener_details_checked = True
        argument_detected = _edge_browser_has_debug_argument(set(browser_ids)) if browser_ids else False
        for checkpoint in checkpoints:
            if checkpoint not in logged and elapsed >= checkpoint:
                alive = bool(browser_ids)
                if logger is not None:
                    checkpoint_label = f"{checkpoint:g}"
                    logger.info("New Edge alive after %ss: %s", checkpoint_label, "yes" if alive else "no")
                    logger.info("Edge main process: %s", "yes" if alive else "no")
                    logger.info("--remote-debugging-port=9222 present: %s", "yes" if argument_detected else "no")
                    logger.info(
                        "9222 TCP LISTEN: %s; owners: %s",
                        "yes" if port_listening else "no",
                        ", ".join(
                            f"{item.local_address}:{item.local_port}/PID {item.owning_pid}/{item.process_name or '<unknown>'}"
                            for item in listeners
                        ) or "<none>",
                    )
                logged.add(checkpoint)
        endpoint = discover_edge_endpoint(user_data_dir)
        if port_listening and not port_status_emitted:
            _emit_status(
                status_callback,
                "9222 已监听，正在验证 CDP JSON……",
                "正在验证 127.0.0.1 和 localhost 的 /json/version、/json/list。",
            )
            port_status_emitted = True
        if endpoint is not None and process_appeared and argument_detected and port_listening:
            probes = probe_edge_cdp(EDGE_DEBUG_PORT)
            if logger is not None:
                logger.info("Remote debugging argument detected: yes")
                logger.info("CDP available: yes")
                for probe in probes:
                    logger.info(
                        "CDP probe %s: status=%s content-type=%s length=%s error=%s",
                        probe.label,
                        probe.status if probe.status is not None else "<none>",
                        probe.content_type or "<none>",
                        probe.content_length if probe.content_length is not None else "<none>",
                        probe.error or "<none>",
                    )
            _emit_status(
                status_callback,
                "Edge 调试接口已开启",
                "Edge 9222 调试接口已验证，可以连接当前正常 Profile。",
            )
            return endpoint
        if elapsed >= checkpoints[-1] and not process_appeared:
            return_code = process.poll()
            diagnostic = write_edge_cdp_diagnostic(
                executable=executable,
                user_data_dir=user_data_dir,
                debug_dir=debug_dir,
                process_ids_before=before_snapshot,
                process_ids_after=current_snapshot,
                listeners_before=listeners_before,
                listeners_after=listeners,
                popen_pid=process.pid,
                stage="no-browser-process-after-popen",
                error=f"Popen return code {return_code}",
            )
            if logger is not None:
                logger.error("Popen return/error: return code %s; no Edge Browser process appeared", return_code)
                logger.info("CDP available: no")
                logger.error("Saved Edge CDP diagnostic: %s", diagnostic)
            raise BrowserConnectionError(
                f"Popen 已执行，但 5 秒内没有出现 Edge Browser 主进程。\n诊断：{diagnostic}",
                status_text="Edge 已关闭，但重新启动失败",
            )
        time.sleep(0.4)

    current_snapshot = _edge_process_command_lines()
    browser_ids = edge_browser_process_ids()
    edge_running = bool(browser_ids)
    detected = _edge_browser_has_debug_argument(set(browser_ids)) if browser_ids else False
    listeners = _tcp_listener_details()
    port_listening = bool(listeners) or _port_is_listening(EDGE_DEBUG_HOST) or _port_is_listening("localhost")
    probes = probe_edge_cdp(EDGE_DEBUG_PORT)
    diagnostic = write_edge_cdp_diagnostic(
        executable=executable,
        user_data_dir=user_data_dir,
        debug_dir=debug_dir,
        process_ids_before=before_snapshot,
        process_ids_after=current_snapshot,
        listeners_before=listeners_before,
        listeners_after=listeners,
        popen_pid=process.pid,
        stage="timeout-waiting-for-cdp",
        error="timeout waiting for validated Edge CDP",
    )
    if logger is not None:
        logger.info("Edge running: %s", "yes" if edge_running else "no")
        logger.info("CDP 9222: %s", "yes" if port_listening else "no")
        logger.info("CDP available: no")
        logger.info("Remote debugging argument detected: %s", "yes" if detected else "no")
        for probe in probes:
            logger.info(
                "CDP probe %s: status=%s content-type=%s length=%s error=%s",
                probe.label,
                probe.status if probe.status is not None else "<none>",
                probe.content_type or "<none>",
                probe.content_length if probe.content_length is not None else "<none>",
                probe.error or "<none>",
            )
        logger.error("Saved Edge CDP diagnostic: %s", diagnostic)
        logger.error("Popen return/error: return code %s", process.poll())
    if edge_running:
        detail = (
            "启动命令中的远程调试参数没有出现在 Browser 主进程命令行中，可能被其他 Edge 实例接管。"
            if not detected
            else "Browser 主进程包含 --remote-debugging-port=9222，但 9222 或 /json/version 未返回有效 DevTools 数据。"
        )
        raise BrowserConnectionError(
            f"Microsoft Edge 已重新启动，但远程调试接口没有成功开启。\n\n{detail}\n\n诊断：{diagnostic}",
            status_text="Edge 已重新启动，但调试接口未开启",
        )
    raise BrowserConnectionError(
        f"Microsoft Edge 曾启动但很快退出，且 127.0.0.1:9222 未开启。\n诊断：{diagnostic}",
        status_text="Edge 启动后立即退出",
    )


def ensure_edge_debug_browser(
    playwright: Playwright,
    *,
    edge_executable: Path | str | None = None,
    force_open_heybox: bool = False,
    verify_profile: bool = False,
    logger: logging.Logger | None = None,
    status_callback: StatusCallback | None = None,
    debug_dir: Path | None = None,
) -> EdgeSession:
    """Attach to the user's already-running Edge; never launch or restart it."""

    executable = find_edge_executable(edge_executable)
    if executable is None:
        raise BrowserConnectionError("未找到 Microsoft Edge。请点击“选择 Edge…”并指定 msedge.exe。")
    user_data_dir = normal_edge_user_data_dir()
    endpoint = discover_edge_endpoint(user_data_dir)
    existing_cdp = endpoint is not None
    if endpoint is None:
        if logger is not None:
            logger.info("Edge attach unavailable; no validated current-instance CDP endpoint")
        _emit_status(status_callback, "等待 Edge 远程调试", EDGE_RESTART_MESSAGE)
        raise BrowserConnectionError(EDGE_RESTART_MESSAGE, status_text="等待 Edge 远程调试")
    elif logger is not None:
        logger.info("CDP available: yes (current Edge endpoint %s)", endpoint.safe_label)
    return _build_session(
        playwright,
        endpoint,
        executable,
        existing_cdp=existing_cdp,
        started_by_app=False,
        verify_profile=verify_profile,
        force_open_heybox=force_open_heybox,
        logger=logger,
    )


def restart_edge_debug_browser(
    playwright: Playwright,
    *,
    edge_executable: Path | str | None = None,
    force_open_heybox: bool = False,
    verify_profile: bool = False,
    logger: logging.Logger | None = None,
    status_callback: StatusCallback | None = None,
    debug_dir: Path | None = None,
) -> EdgeSession:
    """Compatibility entry point: restarting a user browser is intentionally forbidden."""

    raise BrowserConnectionError(
        "程序不会关闭或重新启动你的 Microsoft Edge。请在 edge://inspect/#remote-debugging 中启用当前实例后重新连接。",
        status_text="等待 Edge 远程调试",
    )
