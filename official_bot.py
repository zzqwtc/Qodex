#!/usr/bin/env python3
"""Official QQ robot platform bridge for natural-language Codex control."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

try:
    import botpy
    from botpy import logging as bot_logging
    from botpy.message import C2CMessage, GroupMessage, Message
except ModuleNotFoundError:  # pragma: no cover - dependency is installed on Ubuntu host.
    botpy = None  # type: ignore[assignment]
    bot_logging = None  # type: ignore[assignment]
    C2CMessage = Any  # type: ignore[misc,assignment]
    GroupMessage = Any  # type: ignore[misc,assignment]
    Message = Any  # type: ignore[misc,assignment]

from bridge import (
    BehaviorConfig,
    CodexConfig,
    build_codex_command,
    build_codex_prompt,
    chunk_text,
    classify_intent,
    compact_preview,
    format_elapsed,
    summarize_codex_output,
)


BaseClient = botpy.Client if botpy is not None else object


@dataclass
class OfficialQQConfig:
    appid: str
    secret: str
    allow_all_users: bool = False
    allowed_openids: set[str] = field(default_factory=set)
    bootstrap_secret: str = ""
    allowed_group_openids: set[str] = field(default_factory=set)
    listen_public_messages: bool = True
    listen_guild_channels: bool = False


@dataclass
class OfficialConfig:
    official_qq: OfficialQQConfig
    codex: CodexConfig
    behavior: BehaviorConfig


@dataclass
class ReplyTarget:
    kind: str
    user_openid: str
    msg_id: str
    group_openid: str = ""
    channel_id: str = ""
    guild_id: str = ""

    @property
    def conversation_key(self) -> str:
        if self.kind == "group":
            return f"group:{self.group_openid}:user:{self.user_openid}"
        if self.kind == "guild":
            return f"guild:{self.guild_id}:channel:{self.channel_id}:user:{self.user_openid}"
        return f"c2c:{self.user_openid}"


@dataclass
class PendingApproval:
    prompt: str
    created_at: float


class OfficialCodexClient(BaseClient):  # type: ignore[misc,valid-type]
    def __init__(self, config: OfficialConfig, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.pending: dict[str, PendingApproval] = {}
        self.running_task: asyncio.Task[None] | None = None
        self.running_process: asyncio.subprocess.Process | None = None
        self.running_key: str | None = None
        self.running_started_at: float | None = None
        self.running_prompt_preview: str = ""
        self.running_launching: bool = False
        self.msg_seq_by_message_id: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.log = bot_logging.get_logger() if bot_logging is not None else None

    async def on_ready(self) -> None:
        if self.log:
            self.log.info(f"robot 「{self.robot.name}」 on_ready")

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        target = ReplyTarget(
            kind="c2c",
            user_openid=str(message.author.user_openid or ""),
            msg_id=str(message.id or ""),
        )
        await self._handle_message(target, str(message.content or ""))

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        target = ReplyTarget(
            kind="group",
            user_openid=str(message.author.member_openid or ""),
            group_openid=str(message.group_openid or ""),
            msg_id=str(message.id or ""),
        )
        await self._handle_message(target, str(message.content or ""))

    async def on_at_message_create(self, message: Message) -> None:
        target = ReplyTarget(
            kind="guild",
            user_openid=str(message.author.id or ""),
            msg_id=str(message.id or ""),
            channel_id=str(message.channel_id or ""),
            guild_id=str(message.guild_id or ""),
        )
        await self._handle_message(target, str(message.content or ""))

    async def _handle_message(self, target: ReplyTarget, content: str) -> None:
        content = clean_official_content(content)
        if not content:
            return

        if not self._authorized(target):
            await self._maybe_bootstrap(target, content)
            self._log_unauthorized(target)
            return

        intent = classify_intent(content)
        if intent.name == "noop":
            return

        if intent.name == "stop":
            await self._stop_running(target)
            return

        if intent.name == "cancel":
            had_pending = self.pending.pop(target.conversation_key, None) is not None
            await self._send(target, "已取消待确认任务。" if had_pending else "当前没有待确认任务。")
            return

        if intent.name == "approval":
            pending = self.pending.pop(target.conversation_key, None)
            if pending is None:
                await self._send(target, "当前没有待确认任务。")
                return
            await self._start_codex(target, pending.prompt, read_only=False)
            return

        if intent.name == "running_status":
            await self._send_running_status(target)
            return

        if intent.name == "status":
            prompt = (
                "只读检查当前项目进展。请在不修改文件的前提下总结："
                "1) 当前仓库结构和最近可见工作状态；"
                "2) 已完成内容和仍未完成的关键事项；"
                "3) 明确的下一步建议；"
                f"用户原话：{content}"
            )
            await self._start_codex(target, prompt, read_only=True)
            return

        if intent.name == "task":
            if intent.risky or not self.config.behavior.auto_run_safe_tasks:
                self.pending[target.conversation_key] = PendingApproval(
                    prompt=intent.prompt,
                    created_at=time.time(),
                )
                await self._send(
                    target,
                    "这条指令可能会改动代码、执行命令或触发高风险操作。"
                    "如果确认要做，请回复“可以继续”；如果不做，回复“取消”。",
                )
                return
            await self._start_codex(target, intent.prompt, read_only=intent.read_only)

    def _authorized(self, target: ReplyTarget) -> bool:
        official = self.config.official_qq
        if target.kind == "group" and official.allowed_group_openids:
            if target.group_openid not in official.allowed_group_openids:
                return False
        return official.allow_all_users or target.user_openid in official.allowed_openids

    async def _maybe_bootstrap(self, target: ReplyTarget, content: str) -> None:
        secret = self.config.official_qq.bootstrap_secret.strip()
        if not secret or content.strip() != secret:
            return
        if target.kind == "group":
            await self._send(
                target,
                f"member_openid={target.user_openid}\ngroup_openid={target.group_openid}",
            )
            return
        if target.kind == "guild":
            await self._send(
                target,
                f"user_id={target.user_openid}\nguild_id={target.guild_id}\nchannel_id={target.channel_id}",
            )
            return
        await self._send(target, f"user_openid={target.user_openid}")

    def _log_unauthorized(self, target: ReplyTarget) -> None:
        text = (
            "unauthorized QQ message: "
            f"kind={target.kind} user_openid={target.user_openid} "
            f"group_openid={target.group_openid} guild_id={target.guild_id}"
        )
        if self.log:
            self.log.info(text)
        else:
            print(text, file=sys.stderr)

    async def _start_codex(self, target: ReplyTarget, user_prompt: str, read_only: bool) -> None:
        async with self.lock:
            already_running = (
                self.running_launching
                or self.running_process is not None
                or (self.running_task is not None and not self.running_task.done())
            )
            if not already_running:
                prompt = build_codex_prompt(self.config, user_prompt, read_only=read_only)  # type: ignore[arg-type]
                self.running_key = target.conversation_key
                self.running_started_at = time.time()
                self.running_prompt_preview = compact_preview(user_prompt)
                self.running_launching = True
                self.running_task = asyncio.create_task(self._run_codex_task(target, prompt))

        if already_running:
            await self._send_running_status(target)

    async def _stop_running(self, target: ReplyTarget) -> None:
        async with self.lock:
            if self.running_process is None and not self.running_launching:
                self.pending.pop(target.conversation_key, None)
                await self._send(target, "当前没有正在运行的 Codex 任务，已清理待确认任务。")
                return
            if self.running_key != target.conversation_key:
                await self._send(target, "有任务正在运行，但它不是由这个会话启动的。")
                return
            self.running_process.terminate()
        await self._send(target, "已请求停止当前 Codex 任务。")

    async def _send_running_status(self, target: ReplyTarget) -> None:
        if self.running_process is None and not self.running_launching:
            await self._send(target, "当前没有正在运行的 Codex 任务。")
            return
        if self.running_key != target.conversation_key:
            await self._send(target, "Codex 正在处理另一个会话启动的任务。")
            return
        elapsed = format_elapsed(time.time() - self.running_started_at) if self.running_started_at else "未知时长"
        await self._send(target, f"Codex 还在处理，已运行 {elapsed}。\n当前任务：{self.running_prompt_preview}")

    async def _run_codex_task(self, target: ReplyTarget, prompt: str) -> None:
        workspace = self.config.codex.workspace
        if not workspace.exists():
            await self._send(target, f"Codex 工作目录不存在：{workspace}")
            self._clear_running_state()
            return

        command, stdin_prompt = build_codex_command(self.config, prompt)  # type: ignore[arg-type]
        if self.config.behavior.send_start_ack:
            await self._send(target, "已收到，开始交给 Codex 处理。")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE if stdin_prompt is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            await self._send(target, f"无法启动 Codex：{exc}")
            self._clear_running_state()
            return

        self.running_process = process
        self.running_launching = False
        output_lines: list[str] = []
        last_progress_at = time.time()

        try:
            if stdin_prompt is not None:
                assert process.stdin is not None
                process.stdin.write((stdin_prompt + "\n").encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()

            assert process.stdout is not None
            while True:
                now = time.time()
                if self.config.behavior.send_heartbeat and now - last_progress_at >= 60:
                    elapsed = format_elapsed(now - self.running_started_at) if self.running_started_at else "未知时长"
                    await self._send(target, f"Codex 仍在处理，已运行 {elapsed}，稍后继续回报结果。")
                    last_progress_at = now

                try:
                    raw_line = await asyncio.wait_for(process.stdout.readline(), timeout=2)
                except asyncio.TimeoutError:
                    if process.returncode is not None:
                        break
                    continue
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    output_lines.append(line)

            return_code = await process.wait()
        finally:
            self._clear_running_state()

        output = "\n".join(output_lines).strip()
        await self._send_long(target, summarize_codex_output(output, return_code))

    def _clear_running_state(self) -> None:
        self.running_process = None
        self.running_key = None
        self.running_started_at = None
        self.running_prompt_preview = ""
        self.running_launching = False

    async def _send_long(self, target: ReplyTarget, message: str) -> None:
        limit = max(200, self.config.behavior.max_message_chars)
        for chunk in chunk_text(message, limit):
            await self._send(target, chunk)

    async def _send(self, target: ReplyTarget, content: str) -> None:
        msg_seq = self._next_msg_seq(target)
        if target.kind == "group":
            kwargs = {
                "group_openid": target.group_openid,
                "msg_type": 0,
                "msg_id": target.msg_id,
                "msg_seq": msg_seq,
                "content": content,
            }
            await call_with_optional_msg_seq(self.api.post_group_message, kwargs)
            return
        if target.kind == "guild":
            await self.api.post_message(
                channel_id=target.channel_id,
                msg_id=target.msg_id,
                content=content,
            )
            return
        kwargs = {
            "openid": target.user_openid,
            "msg_type": 0,
            "msg_id": target.msg_id,
            "msg_seq": msg_seq,
            "content": content,
        }
        await call_with_optional_msg_seq(self.api.post_c2c_message, kwargs)

    def _next_msg_seq(self, target: ReplyTarget) -> int:
        key = f"{target.kind}:{target.msg_id}"
        value = self.msg_seq_by_message_id.get(key, 0) + 1
        self.msg_seq_by_message_id[key] = value
        if len(self.msg_seq_by_message_id) > 1000:
            self.msg_seq_by_message_id.clear()
            self.msg_seq_by_message_id[key] = value
        return value


def clean_official_content(content: str) -> str:
    content = re.sub(r"<@!?\d+>", "", content)
    content = re.sub(r"\s+", " ", content)
    return content.strip()


async def call_with_optional_msg_seq(function: Any, kwargs: dict[str, Any]) -> Any:
    try:
        return await function(**kwargs)
    except TypeError as exc:
        if "msg_seq" not in str(exc) and "msgseq" not in str(exc).lower():
            raise
        fallback = dict(kwargs)
        fallback.pop("msg_seq", None)
        return await function(**fallback)


def resolve_config_value(value: Any) -> str:
    text = str(value or "")
    if text.startswith("env:"):
        env_name = text[4:].strip()
        return os_environ(env_name)
    return text


def os_environ(name: str) -> str:
    import os

    return os.environ.get(name, "")


def load_official_config(path: Path) -> OfficialConfig:
    if tomllib is None:
        raise RuntimeError("Python 3.11+ is required for TOML config loading.")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    official_raw = raw.get("official_qq", {})
    codex_raw = raw.get("codex", {})
    behavior_raw = raw.get("behavior", {})

    config = OfficialConfig(
        official_qq=OfficialQQConfig(
            appid=resolve_config_value(official_raw.get("appid", "")),
            secret=resolve_config_value(official_raw.get("secret", "")),
            allow_all_users=bool(official_raw.get("allow_all_users", False)),
            allowed_openids={str(item) for item in official_raw.get("allowed_openids", [])},
            bootstrap_secret=resolve_config_value(official_raw.get("bootstrap_secret", "")),
            allowed_group_openids={str(item) for item in official_raw.get("allowed_group_openids", [])},
            listen_public_messages=bool(official_raw.get("listen_public_messages", True)),
            listen_guild_channels=bool(official_raw.get("listen_guild_channels", False)),
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
    validate_official_config(config)
    return config


def validate_official_config(config: OfficialConfig) -> None:
    if not config.official_qq.appid or not config.official_qq.secret:
        raise ValueError("official_qq.appid and official_qq.secret are required.")
    if not config.official_qq.allow_all_users and not config.official_qq.allowed_openids:
        if not config.official_qq.bootstrap_secret:
            raise ValueError(
                "Set official_qq.allowed_openids, or set a temporary bootstrap_secret to discover openids."
            )
    if config.official_qq.bootstrap_secret and len(config.official_qq.bootstrap_secret) < 16:
        raise ValueError("official_qq.bootstrap_secret should be at least 16 characters.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Official QQ robot to Codex bridge.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.official.toml"),
        help="Path to config.official.toml.",
    )
    parser.add_argument("--show-config", action="store_true", help="Validate and print sanitized config.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        config = load_official_config(args.config)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.show_config:
        sanitized = {
            "official_qq": {
                "appid": config.official_qq.appid,
                "secret": "***",
                "allow_all_users": config.official_qq.allow_all_users,
                "allowed_openids": sorted(config.official_qq.allowed_openids),
                "allowed_group_openids": sorted(config.official_qq.allowed_group_openids),
            },
            "codex": {
                "workspace": str(config.codex.workspace),
                "command": config.codex.command,
                "args": config.codex.args,
                "sandbox": config.codex.sandbox,
            },
        }
        print(json.dumps(sanitized, ensure_ascii=False, indent=2))
        return 0

    if botpy is None:
        print("Missing dependency: pip install -r requirements-official.txt", file=sys.stderr)
        return 2

    intents = botpy.Intents(
        public_messages=config.official_qq.listen_public_messages,
        public_guild_messages=config.official_qq.listen_guild_channels,
    )
    client = OfficialCodexClient(config=config, intents=intents)

    def shutdown(signum: int, _frame: Any) -> None:
        print(f"received signal {signum}, exiting")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    client.run(appid=config.official_qq.appid, secret=config.official_qq.secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
