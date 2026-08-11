from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable


class McpClientError(RuntimeError):
    pass


class McpRequestError(McpClientError):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.data = data


WaitingCallback = Callable[[], None]
WaitingCheck = Callable[[], bool]


class StdioMcpClient:
    """Small JSON-RPC/MCP client for one process connected over stdio."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        logger: logging.Logger,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.logger = logger
        self.environment = environment or {}
        self.process: subprocess.Popen[str] | None = None
        self.server_info: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any] | BaseException]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closing = False

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def is_initialized(self) -> bool:
        return self.is_running and bool(self.server_info) and bool(self.tools)

    def start(self) -> None:
        if self.is_running:
            return
        environment = os.environ.copy()
        environment.update(self.environment)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except OSError as error:
            raise McpClientError(f"无法启动 MCP sidecar：{error}") from error
        threading.Thread(target=self._read_stdout, name="mcp-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="mcp-stderr", daemon=True).start()

    def initialize(self, timeout: float = 60.0) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "HeyboxPostExporter", "version": "0.1.0"},
            },
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise McpClientError("MCP initialize 返回了无效结果。")
        self.server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        self.notify("notifications/initialized", {})
        listed = self.request("tools/list", {}, timeout=timeout)
        tool_items = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = {
            str(item["name"]): item
            for item in tool_items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        return result

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 600.0,
        waiting_after: float | None = None,
        waiting_check: WaitingCheck | None = None,
        on_waiting: WaitingCallback | None = None,
    ) -> dict[str, Any]:
        if name not in self.tools:
            raise McpClientError(f"MCP sidecar 未提供工具：{name}")
        result = self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
            waiting_after=waiting_after,
            waiting_check=waiting_check,
            on_waiting=on_waiting,
        )
        if not isinstance(result, dict):
            raise McpClientError(f"MCP 工具 {name} 返回了无效结果。")
        if result.get("isError"):
            raise McpClientError(self.extract_text(result) or f"MCP 工具 {name} 执行失败。")
        return result

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        waiting_after: float | None = None,
        waiting_check: WaitingCheck | None = None,
        on_waiting: WaitingCallback | None = None,
    ) -> Any:
        if not self.is_running or self.process is None or self.process.stdin is None:
            raise McpClientError("MCP sidecar 未运行。")
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            self._write(payload)
            started = time.monotonic()
            waiting_emitted = False
            while True:
                elapsed = time.monotonic() - started
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise McpClientError(f"MCP 请求 {method} 等待超时。")
                try:
                    message = response_queue.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    if (
                        not waiting_emitted
                        and waiting_after is not None
                        and elapsed >= waiting_after
                        and on_waiting is not None
                        and (waiting_check is None or waiting_check())
                    ):
                        waiting_emitted = True
                        on_waiting()
                    continue
                if isinstance(message, BaseException):
                    raise McpClientError(str(message)) from message
                error = message.get("error")
                if isinstance(error, dict):
                    raise McpRequestError(
                        int(error.get("code") or -1),
                        str(error.get("message") or "unknown error"),
                        error.get("data"),
                    )
                return message.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    @staticmethod
    def extract_text(result: dict[str, Any]) -> str:
        content = result.get("content")
        if not isinstance(content, list):
            return ""
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        self._closing = True
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            if os.name == "nt" and process.pid:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
        self._fail_pending(McpClientError("MCP sidecar 已关闭。"))
        self.process = None

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise McpClientError("MCP sidecar stdin 不可用。")
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                self.process.stdin.write(wire)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise McpClientError("MCP sidecar stdio 已断开。") from error

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.logger.warning("MCP stdout 收到非 JSON 数据，已忽略。")
                continue
            request_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(request_id, int):
                continue
            with self._pending_lock:
                response_queue = self._pending.get(request_id)
            if response_queue is not None:
                response_queue.put(message)
        if not self._closing:
            try:
                code = process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                code = process.poll()
            self._fail_pending(McpClientError(f"MCP sidecar 已退出（code={code}）。"))

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            text = line.strip()
            if not text:
                continue
            # npm failures are useful; never mirror verbose browser protocol data.
            lowered = text.casefold()
            if "npm error" in lowered or "error:" in lowered or "failed" in lowered:
                self.logger.warning("MCP sidecar: %s", text[:500])

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            queues = list(self._pending.values())
        for response_queue in queues:
            try:
                response_queue.put_nowait(error)
            except queue.Full:
                pass


def resolve_node_and_npx() -> tuple[str, str, str]:
    node = shutil.which("node.exe") or shutil.which("node")
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not node or not npx:
        raise McpClientError("未检测到系统 Node.js / npx。请先安装 Node.js，并确保 node 和 npx 位于 PATH。")
    try:
        completed = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise McpClientError(f"无法执行系统 Node.js：{error}") from error
    version = (completed.stdout or "").strip()
    if completed.returncode != 0 or not version:
        raise McpClientError("系统 Node.js 检测失败。")
    return node, npx, version
