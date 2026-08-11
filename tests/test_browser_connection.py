import json
from pathlib import Path

import pytest

import heybox_exporter.browser_connection as browser_connection
from heybox_exporter.browser_connection import (
    BrowserConnectionError,
    CdpEndpoint,
    EndpointProbe,
    EDGE_DEBUG_PORT,
    TcpListener,
    choose_work_page,
    discover_edge_endpoint,
    edge_browser_process_ids,
    edge_launch_command,
    ensure_edge_debug_browser,
    find_edge_executable,
    parse_devtools_active_port,
    valid_edge_endpoint,
)


def test_parse_devtools_active_port_uses_dynamic_port_and_websocket(tmp_path: Path) -> None:
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("51437\n/devtools/browser/abc-123\n", encoding="ascii")

    endpoint = parse_devtools_active_port(active_port, tmp_path)

    assert endpoint is not None
    assert endpoint.port == 51437
    assert endpoint.endpoint_url == "ws://127.0.0.1:51437/devtools/browser/abc-123"
    assert endpoint.user_data_dir == tmp_path.resolve()


def test_stale_default_devtools_active_port_is_rejected(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "DevToolsActivePort").write_text("51437\n/devtools/browser/stale\n", encoding="ascii")
    monkeypatch.setattr(browser_connection, "_probe_version", lambda _port: None)

    assert valid_edge_endpoint(tmp_path) is None


def test_edge_lookup_prefers_program_files_x86(tmp_path: Path, monkeypatch) -> None:
    x86 = tmp_path / "x86"
    x64 = tmp_path / "x64"
    preferred = x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    fallback = x64 / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    preferred.parent.mkdir(parents=True)
    fallback.parent.mkdir(parents=True)
    preferred.touch()
    fallback.touch()
    monkeypatch.setenv("ProgramFiles(x86)", str(x86))
    monkeypatch.setenv("ProgramFiles", str(x64))

    assert find_edge_executable() == preferred.resolve()


def test_edge_launch_command_never_contains_user_data_dir(tmp_path: Path) -> None:
    executable = tmp_path / "msedge.exe"

    command = edge_launch_command(executable)

    assert command == (str(executable),)
    assert all("--remote-debugging" not in argument for argument in command)


def test_browser_process_detection_excludes_edge_children(monkeypatch) -> None:
    monkeypatch.setattr(
        browser_connection,
        "_edge_process_command_lines",
        lambda: [
            (10, "msedge.exe --remote-debugging-port=9222"),
            (20, "msedge.exe --type=renderer --user-data-dir=ignored"),
            (30, "msedge.exe --type=gpu-process"),
        ],
    )

    assert edge_browser_process_ids() == [10]


def test_devtools_active_port_is_preferred_and_validated(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "DevToolsActivePort").write_text(
        "51437\n/devtools/browser/current-instance\n",
        encoding="ascii",
    )
    monkeypatch.setattr(
        browser_connection,
        "_probe_version",
        lambda port: {
            "Browser": "Microsoft Edge/150",
            "Protocol-Version": "1.3",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/id",
        },
    )

    endpoint = discover_edge_endpoint(tmp_path)

    assert endpoint is not None
    assert endpoint.port == 51437
    assert endpoint.endpoint_url == "ws://127.0.0.1:51437/devtools/browser/id"
    assert endpoint.source.startswith("DevToolsActivePort")


def test_fixed_port_rejects_non_cdp_or_non_edge_listener(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(browser_connection, "_probe_version", lambda _port: {"Browser": "Microsoft Edge/150"})

    assert discover_edge_endpoint(tmp_path) is None


def test_cdp_probe_checks_both_hosts_and_both_json_endpoints(monkeypatch) -> None:
    seen: list[tuple[str, str, int]] = []

    def fake_probe(host: str, path: str, *, port: int, timeout: float) -> EndpointProbe:
        seen.append((host, path, port))
        return EndpointProbe(host, path, port, 200, "application/json", 2, {})

    monkeypatch.setattr(browser_connection, "_probe_json_endpoint", fake_probe)

    probes = browser_connection.probe_edge_cdp()

    assert seen == [
        ("127.0.0.1", "/json/version", EDGE_DEBUG_PORT),
        ("127.0.0.1", "/json/list", EDGE_DEBUG_PORT),
        ("localhost", "/json/version", EDGE_DEBUG_PORT),
        ("localhost", "/json/list", EDGE_DEBUG_PORT),
    ]
    assert all(probe.ok for probe in probes)


def test_non_edge_owner_of_fixed_port_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(
        browser_connection,
        "_tcp_listener_details",
        lambda _port=EDGE_DEBUG_PORT: [TcpListener("127.0.0.1", EDGE_DEBUG_PORT, 4242, "python")],
    )
    monkeypatch.setattr(browser_connection, "_port_is_listening", lambda *_args: True)
    monkeypatch.setattr(browser_connection, "edge_process_ids", lambda: [])

    with pytest.raises(BrowserConnectionError, match="PID 4242.*python"):
        browser_connection._raise_if_fixed_port_is_unavailable()


def test_explicit_restart_entry_point_never_closes_edge(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "msedge.exe"
    executable.touch()
    endpoint = CdpEndpoint(tmp_path, "http://127.0.0.1:9222", "127.0.0.1", 9222, "existing")
    monkeypatch.setattr(
        browser_connection,
        "_shutdown_edge_for_restart",
        lambda *_args: pytest.fail("must not shut down Edge"),
    )

    with pytest.raises(BrowserConnectionError, match="不会关闭或重新启动"):
        browser_connection.restart_edge_debug_browser(object(), edge_executable=executable)  # type: ignore[arg-type]


def test_shutdown_helper_is_disabled(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "msedge.exe"
    states = iter([[10, 20], [10], []])
    latest = [10, 20]

    def process_ids() -> list[int]:
        nonlocal latest
        try:
            latest = next(states)
        except StopIteration:
            pass
        return latest

    monkeypatch.setattr(browser_connection, "edge_process_ids", process_ids)
    monkeypatch.setattr(browser_connection, "edge_browser_process_ids", lambda: [10])
    monkeypatch.setattr(browser_connection, "request_normal_edge_exit", lambda _pids: True)
    monkeypatch.setattr(browser_connection, "_wait_for_normal_edge_exit", lambda _logger: False)
    monkeypatch.setattr(browser_connection, "_force_terminate_edge_processes", lambda _logger, **_kwargs: True)

    with pytest.raises(BrowserConnectionError, match="不会关闭或重新启动"):
        browser_connection._shutdown_edge_for_restart(executable, None, None)


def test_running_edge_without_cdp_requires_current_instance_debugging(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "msedge.exe"
    executable.touch()
    monkeypatch.setattr(browser_connection, "valid_edge_endpoint", lambda _path=None: None)
    monkeypatch.setattr(browser_connection, "edge_browser_process_ids", lambda: [1234])

    with pytest.raises(BrowserConnectionError, match="不会关闭或重新启动"):
        ensure_edge_debug_browser(object(), edge_executable=executable)  # type: ignore[arg-type]


class _FakePage:
    def __init__(self, url: str, name: str = "") -> None:
        self.url = url
        self.name = name
        self.navigations: list[str] = []

    def goto(self, url: str) -> None:
        self.url = url
        self.navigations.append(url)

    def evaluate(self, expression: str, argument: str | None = None) -> str | None:
        if argument is None:
            return self.name
        self.name = argument
        return None


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.created: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage("about:blank")
        self.pages.append(page)
        self.created.append(page)
        return page


def test_work_page_never_reuses_or_navigates_existing_user_tab() -> None:
    user_page = _FakePage("https://www.xiaoheihe.cn/app/bbs/link/1")
    context = _FakeContext([user_page])

    work_page = choose_work_page(context)  # type: ignore[arg-type]

    assert work_page is context.created[0]
    assert user_page.navigations == []
    assert work_page.name == browser_connection.WORK_PAGE_MARKER


def test_last_used_profile_is_read_without_creating_or_copying_data(tmp_path: Path) -> None:
    (tmp_path / "Local State").write_text(
        json.dumps({"profile": {"last_used": "Profile 2"}}),
        encoding="utf-8",
    )

    profile_path, source = browser_connection._last_used_profile_path(tmp_path)

    assert profile_path == (tmp_path / "Profile 2").resolve()
    assert source == "Edge Local State"
    assert not profile_path.exists()
