#!/usr/bin/env python3
"""Natural-language QQ to Codex bridge for OneBot HTTP callbacks."""

from __future__ import annotations

import argparse
import json
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]


STATUS_PATTERNS = (
    "进展",
    "做到哪",
    "现在怎么样",
    "现在什么情况",
    "状态",
    "summary",
    "status",
    "progress",
    "还有哪些",
    "没提交",
)

RUNNING_STATUS_PATTERNS = (
    "运行到哪",
    "跑到哪",
    "处理到哪",
    "执行到哪",
    "任务到哪",
    "还在跑",
    "还在处理",
    "还没回复",
    "没有回复",
    "卡住",
    "在干嘛",
)

READ_ONLY_PATTERNS = (
    "只读",
    "别改",
    "不要改",
    "不改代码",
    "只分析",
    "只给方案",
    "先给方案",
    "看一下",
    "检查一下",
    "review",
)

CHAT_PATTERNS = (
    "你是什么",
    "你是谁",
    "什么模型",
    "哪个模型",
    "介绍一下你",
    "能做什么",
)

APPROVAL_PATTERNS = (
    "可以",
    "同意",
    "批准",
    "继续",
    "执行吧",
    "确认",
    "approve",
    "yes",
    "ok",
)

CANCEL_PATTERNS = (
    "取消",
    "不用了",
    "算了",
    "否",
    "不要",
    "cancel",
    "no",
)

STOP_PATTERNS = (
    "停",
    "暂停",
    "停止",
    "打断",
    "stop",
    "pause",
)

RISK_PATTERNS = (
    "删除",
    "清空",
    "重置",
    "reset",
    "checkout --",
    "rebase",
    "force push",
    "push",
    "提交",
    "commit",
    "安装",
    "install",
    "下载",
    "download",
    "联网",
    "网络",
    "暴露",
    "公网",
    "systemctl",
    "sudo",
    "chmod",
    "chown",
    "rm ",
    "rm -",
    "删掉",
)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    path: str = "/onebot"


@dataclass
class OneBotConfig:
    api_url: str = "http://127.0.0.1:3000"
    access_token: str = ""


@dataclass
class AuthConfig:
    allow_all_users: bool = False
    allowed_user_ids: set[int] = field(default_factory=set)
    allowed_group_ids: set[int] = field(default_factory=set)
    require_at_in_groups: bool = True
    bot_user_id: int = 0


@dataclass
class CodexConfig:
    workspace: Path = Path.cwd()
    sandbox: str = "workspace-write"
    command: str = "codex"
    args: list[str] = field(default_factory=lambda: ["exec", "--sandbox", "{sandbox}", "-"])
    system_context: str = ""


@dataclass
class BehaviorConfig:
    auto_run_safe_tasks: bool = True
    max_message_chars: int = 1400
    send_start_ack: bool = False
    send_heartbeat: bool = False


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)


@dataclass
class ChatTarget:
    message_type: str
    user_id: int
    group_id: int | None = None


@dataclass
class PendingApproval:
    target: ChatTarget
    prompt: str
    created_at: float


@dataclass
class Intent:
    name: str
    prompt: str = ""
    read_only: bool = False
    risky: bool = False


class BridgeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: dict[str, PendingApproval] = {}
        self.running_process: subprocess.Popen[str] | None = None
        self.running_target: ChatTarget | None = None
        self.running_started_at: float | None = None
        self.running_prompt_preview: str = ""
        self.running_launching: bool = False

    def target_key(self, target: ChatTarget) -> str:
        if target.message_type == "group" and target.group_id is not None:
            return f"group:{target.group_id}:user:{target.user_id}"
        return f"private:{target.user_id}"


class QQCodexBridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = BridgeState()

    def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("post_type") != "message":
            return

        target = self._target_from_event(event)
        if target is None:
            return

        message = self._plain_text_message(event.get("message", ""))
        if not message:
            return

        if not self._authorized(event, target, message):
            return

        message = self._strip_bot_mentions(message)
        if not message:
            return

        intent = classify_intent(message)
        if intent.name == "noop":
            return

        if intent.name == "stop":
            self._stop_running(target)
            return

        if intent.name == "cancel":
            self._cancel_pending(target)
            return

        if intent.name == "approval":
            self._approve_pending(target)
            return

        if intent.name == "status":
            prompt = self._status_prompt(message)
            self._start_codex(target, prompt, read_only=True)
            return

        if intent.name == "running_status":
            self._send_running_status(target)
            return

        if intent.name == "task":
            if intent.risky or not self.config.behavior.auto_run_safe_tasks:
                self._record_pending(target, intent.prompt)
                self._send_message(
                    target,
                    "这条指令可能会改动代码、执行命令或触发高风险操作。"
                    "如果确认要做，请回复“可以继续”；如果不做，回复“取消”。",
                )
                return
            self._start_codex(target, intent.prompt, read_only=intent.read_only)
            return

    def _target_from_event(self, event: dict[str, Any]) -> ChatTarget | None:
        message_type = str(event.get("message_type", ""))
        user_id = int(event.get("user_id") or 0)
        if not user_id:
            return None
        if message_type == "group":
            group_id = int(event.get("group_id") or 0)
            if not group_id:
                return None
            return ChatTarget(message_type="group", user_id=user_id, group_id=group_id)
        if message_type == "private":
            return ChatTarget(message_type="private", user_id=user_id)
        return None

    def _authorized(self, event: dict[str, Any], target: ChatTarget, message: str) -> bool:
        auth = self.config.auth
        if not auth.allow_all_users and target.user_id not in auth.allowed_user_ids:
            return False

        if target.message_type == "private":
            return True

        if target.group_id is None:
            return False
        if auth.allowed_group_ids and target.group_id not in auth.allowed_group_ids:
            return False
        if not auth.require_at_in_groups:
            return True

        return self._mentions_bot(event, message)

    def _mentions_bot(self, event: dict[str, Any], message: str) -> bool:
        bot_user_id = self.config.auth.bot_user_id
        if bot_user_id and f"[CQ:at,qq={bot_user_id}]" in message:
            return True

        raw_message = event.get("raw_message", "")
        if bot_user_id and f"[CQ:at,qq={bot_user_id}]" in str(raw_message):
            return True

        reply = event.get("reply")
        if isinstance(reply, dict) and int(reply.get("sender", {}).get("user_id") or 0) == bot_user_id:
            return True

        return False

    def _plain_text_message(self, message: Any) -> str:
        if isinstance(message, str):
            return message.strip()
        if isinstance(message, list):
            parts: list[str] = []
            for item in message:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                data = item.get("data") or {}
                if item_type == "text":
                    parts.append(str(data.get("text", "")))
                elif item_type == "at":
                    parts.append(f"[CQ:at,qq={data.get('qq', '')}]")
            return "".join(parts).strip()
        return str(message or "").strip()

    def _strip_bot_mentions(self, message: str) -> str:
        bot_user_id = self.config.auth.bot_user_id
        if bot_user_id:
            message = message.replace(f"[CQ:at,qq={bot_user_id}]", "")
        message = re.sub(r"\[CQ:[^\]]+\]", "", message)
        return message.strip()

    def _status_prompt(self, message: str) -> str:
        return (
            "只读检查当前项目进展。请在不修改文件的前提下总结："
            "1) 当前仓库结构和最近可见工作状态；"
            "2) 已完成内容和仍未完成的关键事项；"
            "3) 明确的下一步建议；"
            f"用户原话：{message}"
        )

    def _record_pending(self, target: ChatTarget, prompt: str) -> None:
        key = self.state.target_key(target)
        with self.state.lock:
            self.state.pending[key] = PendingApproval(
                target=target,
                prompt=prompt,
                created_at=time.time(),
            )

    def _cancel_pending(self, target: ChatTarget) -> None:
        key = self.state.target_key(target)
        with self.state.lock:
            had_pending = self.state.pending.pop(key, None) is not None
        self._send_message(target, "已取消待确认任务。" if had_pending else "当前没有待确认任务。")

    def _approve_pending(self, target: ChatTarget) -> None:
        key = self.state.target_key(target)
        with self.state.lock:
            pending = self.state.pending.pop(key, None)
        if pending is None:
            self._send_message(target, "当前没有待确认任务。")
            return
        self._start_codex(target, pending.prompt, read_only=False)

    def _stop_running(self, target: ChatTarget) -> None:
        with self.state.lock:
            process = self.state.running_process
            running_target = self.state.running_target
            launching = self.state.running_launching
            if process is None and not launching:
                self.state.pending.pop(self.state.target_key(target), None)
                self._send_message(target, "当前没有正在运行的 Codex 任务，已清理待确认任务。")
                return
            if running_target and self.state.target_key(running_target) != self.state.target_key(target):
                self._send_message(target, "有任务正在运行，但它不是由这个会话启动的。")
                return
            if process is None and launching:
                self._send_message(target, "Codex 正在启动中，等进程建立后会继续处理停止请求。")
                return
            process.terminate()
        self._send_message(target, "已请求停止当前 Codex 任务。")

    def _send_running_status(self, target: ChatTarget) -> None:
        with self.state.lock:
            process = self.state.running_process
            running_target = self.state.running_target
            started_at = self.state.running_started_at
            prompt_preview = self.state.running_prompt_preview
            launching = self.state.running_launching

        if process is None and not launching:
            self._send_message(target, "当前没有正在运行的 Codex 任务。")
            return
        if running_target and self.state.target_key(running_target) != self.state.target_key(target):
            self._send_message(target, "Codex 正在处理另一个会话启动的任务。")
            return
        elapsed = format_elapsed(time.time() - started_at) if started_at else "未知时长"
        self._send_message(target, f"Codex 还在处理，已运行 {elapsed}。\n当前任务：{prompt_preview}")

    def _start_codex(self, target: ChatTarget, user_prompt: str, read_only: bool) -> None:
        with self.state.lock:
            if self.state.running_process is not None or self.state.running_launching:
                already_running = True
            else:
                already_running = False
                self.state.running_launching = True
                self.state.running_target = target
                self.state.running_started_at = time.time()
                self.state.running_prompt_preview = compact_preview(user_prompt)

        if already_running:
            self._send_running_status(target)
            return

        prompt = build_codex_prompt(self.config, user_prompt, read_only=read_only)
        thread = threading.Thread(
            target=self._run_codex_task,
            args=(target, prompt),
            daemon=True,
        )
        thread.start()

    def _clear_running_state(self) -> None:
        self.state.running_process = None
        self.state.running_target = None
        self.state.running_started_at = None
        self.state.running_prompt_preview = ""
        self.state.running_launching = False

    def _run_codex_task(self, target: ChatTarget, prompt: str) -> None:
        command, stdin_prompt = build_codex_command(self.config, prompt)
        workspace = self.config.codex.workspace

        if not workspace.exists():
            self._send_message(target, f"Codex 工作目录不存在：{workspace}")
            with self.state.lock:
                self._clear_running_state()
            return

        if self.config.behavior.send_start_ack:
            self._send_message(target, "已收到，开始交给 Codex 处理。")

        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE if stdin_prompt is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            self._send_message(target, f"无法启动 Codex：{exc}")
            with self.state.lock:
                self._clear_running_state()
            return

        with self.state.lock:
            self.state.running_process = process
            self.state.running_target = target
            self.state.running_launching = False

        output_lines: list[str] = []
        last_progress_at = time.time()
        line_queue: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            assert process.stdout is not None
            for stdout_line in process.stdout:
                line_queue.put(stdout_line.rstrip())
            line_queue.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()

        try:
            assert process.stdout is not None
            if stdin_prompt is not None:
                assert process.stdin is not None
                process.stdin.write(stdin_prompt)
                process.stdin.write("\n")
                process.stdin.close()

            while True:
                now = time.time()
                if self.config.behavior.send_heartbeat and now - last_progress_at >= 60:
                    elapsed = format_elapsed(now - (self.state.running_started_at or now))
                    self._send_message(target, f"Codex 仍在处理，已运行 {elapsed}，稍后继续回报结果。")
                    last_progress_at = now

                try:
                    line = line_queue.get(timeout=2)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                if line:
                    output_lines.append(line)

            return_code = process.wait()
        finally:
            with self.state.lock:
                if self.state.running_process is process:
                    self._clear_running_state()

        output = "\n".join(output_lines).strip()
        summary = summarize_codex_output(output, return_code)
        self._send_long_message(target, summary)

    def _send_long_message(self, target: ChatTarget, message: str) -> None:
        limit = max(200, self.config.behavior.max_message_chars)
        chunks = chunk_text(message, limit)
        for chunk in chunks:
            self._send_message(target, chunk)

    def _send_message(self, target: ChatTarget, message: str) -> None:
        payload: dict[str, Any]
        if target.message_type == "group" and target.group_id is not None:
            endpoint = "send_group_msg"
            payload = {"group_id": target.group_id, "message": message}
        else:
            endpoint = "send_private_msg"
            payload = {"user_id": target.user_id, "message": message}

        try:
            onebot_request(self.config.onebot, endpoint, payload)
        except Exception as exc:  # pragma: no cover - depends on OneBot availability.
            print(f"failed to send QQ message: {exc}", file=sys.stderr)


def load_config(path: Path) -> Config:
    if tomllib is None:
        raise RuntimeError("Python 3.11+ is required for TOML config loading.")

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    server_raw = raw.get("server", {})
    onebot_raw = raw.get("onebot", {})
    auth_raw = raw.get("auth", {})
    codex_raw = raw.get("codex", {})
    behavior_raw = raw.get("behavior", {})

    return Config(
        server=ServerConfig(
            host=str(server_raw.get("host", "127.0.0.1")),
            port=int(server_raw.get("port", 8787)),
            path=str(server_raw.get("path", "/onebot")),
        ),
        onebot=OneBotConfig(
            api_url=str(onebot_raw.get("api_url", "http://127.0.0.1:3000")).rstrip("/"),
            access_token=str(onebot_raw.get("access_token", "")),
        ),
        auth=AuthConfig(
            allow_all_users=bool(auth_raw.get("allow_all_users", False)),
            allowed_user_ids={int(item) for item in auth_raw.get("allowed_user_ids", [])},
            allowed_group_ids={int(item) for item in auth_raw.get("allowed_group_ids", [])},
            require_at_in_groups=bool(auth_raw.get("require_at_in_groups", True)),
            bot_user_id=int(auth_raw.get("bot_user_id", 0)),
        ),
        codex=CodexConfig(
            workspace=Path(str(codex_raw.get("workspace", Path.cwd()))).expanduser(),
            sandbox=str(codex_raw.get("sandbox", "workspace-write")),
            command=str(codex_raw.get("command", "codex")),
            args=[str(item) for item in codex_raw.get("args", ["exec", "--sandbox", "{sandbox}", "-"])],
            system_context=str(codex_raw.get("system_context", "")),
        ),
        behavior=BehaviorConfig(
            auto_run_safe_tasks=bool(behavior_raw.get("auto_run_safe_tasks", True)),
            max_message_chars=int(behavior_raw.get("max_message_chars", 1400)),
            send_start_ack=bool(behavior_raw.get("send_start_ack", False)),
            send_heartbeat=bool(behavior_raw.get("send_heartbeat", False)),
        ),
    )


def validate_config(config: Config) -> None:
    if not config.auth.allow_all_users and not config.auth.allowed_user_ids:
        raise ValueError(
            "auth.allowed_user_ids must not be empty unless auth.allow_all_users is true."
        )
    if config.auth.require_at_in_groups and not config.auth.bot_user_id:
        raise ValueError("auth.bot_user_id is required when require_at_in_groups is true.")


def classify_intent(message: str) -> Intent:
    normalized = normalize_text(message)
    if not normalized:
        return Intent(name="noop")

    if has_any(normalized, STOP_PATTERNS):
        return Intent(name="stop")
    if has_any(normalized, CANCEL_PATTERNS):
        return Intent(name="cancel")
    if is_short_approval(normalized):
        return Intent(name="approval")

    read_only = has_any(normalized, READ_ONLY_PATTERNS) or has_any(normalized, CHAT_PATTERNS)
    risky = has_any(normalized, RISK_PATTERNS)

    if has_any(normalized, RUNNING_STATUS_PATTERNS):
        return Intent(name="running_status", prompt=message, read_only=True)

    if has_any(normalized, STATUS_PATTERNS):
        return Intent(name="status", prompt=message, read_only=True)

    return Intent(name="task", prompt=message, read_only=read_only, risky=risky)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def has_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern.lower() in value for pattern in patterns)


def is_short_approval(value: str) -> bool:
    compact = re.sub(r"[\s，。,.!！?？]", "", value)
    if len(compact) > 12:
        return False
    return any(pattern.lower() == compact for pattern in APPROVAL_PATTERNS) or has_any(value, APPROVAL_PATTERNS)


def compact_preview(text: str, limit: int = 120) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{sec}秒"
    return f"{sec}秒"


def build_codex_prompt(config: Config, user_prompt: str, read_only: bool) -> str:
    mode = (
        "本轮是只读分析任务。不要修改文件，不要运行会写入仓库或系统状态的命令。"
        if read_only
        else "本轮可以在仓库内做必要修改，但保持改动小而聚焦。遇到删除、重置、安装依赖、联网、提交或推送等高风险动作时先停止并说明需要用户确认。"
    )
    context = config.codex.system_context.strip()
    parts = [
        context,
        mode,
        "请用中文给 QQ 用户回复最终结果，简洁说明做了什么、验证了什么、下一步是什么。",
        "用户的自然语言指令如下：",
        user_prompt.strip(),
    ]
    return "\n\n".join(part for part in parts if part)


def build_codex_command(config: Config, prompt: str) -> tuple[list[str], str | None]:
    command = config.codex.command.strip()
    parts = shlex.split(command)
    if not parts:
        parts = ["codex"]

    stdin_prompt: str | None = prompt
    args: list[str] = []
    for item in config.codex.args:
        if item == "{prompt}":
            args.append(prompt)
            stdin_prompt = None
        else:
            args.append(item.replace("{sandbox}", config.codex.sandbox))
    return parts + args, stdin_prompt


def summarize_codex_output(output: str, return_code: int) -> str:
    if not output:
        return "Codex 已结束，但没有输出。" if return_code == 0 else f"Codex 执行失败，退出码 {return_code}，没有输出。"

    cleaned = extract_final_codex_message(output)
    lines = [line for line in cleaned.splitlines() if line.strip()]
    tail = "\n".join(lines[-40:])
    if return_code == 0:
        return tail
    return f"Codex 执行失败，退出码 {return_code}。\n\n{tail}"


def extract_final_codex_message(output: str) -> str:
    lines = [line.rstrip() for line in output.splitlines()]
    lines = strip_codex_cli_noise(lines)

    if "codex" in lines:
        marker = len(lines) - 1 - lines[::-1].index("codex")
        candidate = lines[marker + 1 :]
        candidate = strip_codex_cli_noise(candidate)
        candidate = strip_repeated_tail(candidate)
        if candidate:
            return "\n".join(candidate).strip()

    candidate = strip_repeated_tail(lines)
    return "\n".join(candidate[-40:]).strip()


def strip_codex_cli_noise(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_user_block = False
    skip_after_tokens = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("OpenAI Codex "):
            continue
        if line == "--------":
            continue
        if re.match(r"^(workdir|model|provider|approval|sandbox|reasoning effort|reasoning summaries|session id):", line):
            continue
        if line == "user":
            skip_user_block = True
            continue
        if line == "codex":
            skip_user_block = False
            skip_after_tokens = False
            cleaned.append(line)
            continue
        if line == "tokens used":
            skip_after_tokens = True
            continue
        if skip_after_tokens and re.fullmatch(r"[\d,]+", line):
            continue
        if skip_user_block:
            continue
        cleaned.append(line)

    return cleaned


def strip_repeated_tail(lines: list[str]) -> list[str]:
    if len(lines) < 2:
        return lines

    best: list[str] = lines
    for size in range(1, len(lines) // 2 + 1):
        first = lines[-2 * size : -size]
        second = lines[-size:]
        if first == second:
            best = lines[:-size]
    return best


def chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def onebot_request(config: OneBotConfig, endpoint: str, payload: dict[str, Any]) -> Any:
    url = f"{config.api_url}/{endpoint.lstrip('/')}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.access_token:
        headers["Authorization"] = f"Bearer {config.access_token}"

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body else None


def make_handler(bridge: QQCodexBridge) -> type[BaseHTTPRequestHandler]:
    expected_path = bridge.config.server.path

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming.
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return

            content_length = int(self.headers.get("Content-Length", "0") or 0)
            raw_body = self.rfile.read(content_length)
            try:
                event = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            bridge.handle_event(event)
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming.
            if urllib.parse.urlparse(self.path).path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}")

    return Handler


def run_server(config: Config) -> None:
    bridge = QQCodexBridge(config)
    handler = make_handler(bridge)
    server = ThreadingHTTPServer((config.server.host, config.server.port), handler)

    def shutdown(signum: int, _frame: Any) -> None:
        print(f"received signal {signum}, shutting down")
        server.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(
        f"QQ Codex bridge listening on http://{config.server.host}:{config.server.port}{config.server.path}",
        flush=True,
    )
    server.serve_forever()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Natural-language QQ to Codex bridge.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.toml"),
        help="Path to config.toml.",
    )
    parser.add_argument(
        "--classify",
        metavar="TEXT",
        help="Classify one message and print the internal intent, then exit.",
    )
    parser.add_argument(
        "--summarize-output-file",
        type=Path,
        help="Read a raw Codex CLI output file, summarize it, and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.classify is not None:
        intent = classify_intent(args.classify)
        print(json.dumps(intent.__dict__, ensure_ascii=False, indent=2))
        return 0
    if args.summarize_output_file is not None:
        output = args.summarize_output_file.read_text(encoding="utf-8", errors="replace")
        print(summarize_codex_output(output, 0))
        return 0

    config = load_config(args.config)
    validate_config(config)
    run_server(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
