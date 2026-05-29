#!/usr/bin/env python3
"""Telegram <-> Claude Code bridge.

Forwards Telegram messages to `claude -p` (Claude Code headless mode) and sends
the result back. Supports model switching, voice input, multi-user auth,
session continuity (--resume), and abort.

Architecture:
    Telegram user -> bot API -> bridge.py -> `claude -p` (stream-json) -> back

Adapted from zo-ll/pi-bridge, which targeted the `pi` coding agent's RPC mode.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Voice transcription is optional. If faster-whisper isn't installed, the bridge
# still runs for text; voice messages just get a "disabled" reply.
try:
    from faster_whisper import WhisperModel

    WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
except ImportError:
    WHISPER_MODEL = None

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_DIR = Path(
    os.environ.get("BRIDGE_CONFIG_DIR", Path.home() / ".claude-bridge")
).expanduser()
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = CONFIG_DIR / "telegram-token"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Telegram user IDs allowed to use the bot. EMPTY = anyone (dangerous: anyone
# who finds the bot gets a coding agent on your VPS). Always set this.
ALLOWED_USERS = set(
    int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()
)

# Directory Claude Code runs in. It can read / write / run commands here, so
# point it at a dedicated workspace, not your home dir.
WORKSPACE = Path(os.environ.get("CLAUDE_WORKSPACE", Path.cwd())).expanduser()

# Permission posture. No human is present to approve tool prompts in headless
# mode, so a tool that isn't pre-approved ABORTS the run. Options:
#   acceptEdits       - auto-approve file edits + safe fs commands (default)
#   dontAsk           - only permissions.allow rules + read-only command set
#   bypassPermissions - approve EVERYTHING (full shell on the VPS; dangerous)
#   plan / default    - see Claude Code permission-modes docs
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")

# Extra tools to pre-approve, comma- or space-separated, e.g. "Bash,Read,Edit".
ALLOWED_TOOLS = os.environ.get("CLAUDE_ALLOWED_TOOLS", "").strip()

# Skip project/user context (CLAUDE.md, MCP, hooks, skills) for fast, identical
# runs. Bare mode skips OAuth/keychain and REQUIRES ANTHROPIC_API_KEY.
BARE = os.environ.get("CLAUDE_BARE", "").lower() in ("1", "true", "yes")

DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")

KNOWN_MODELS = {
    "opus": "most capable",
    "sonnet": "balanced (default)",
    "haiku": "fastest",
}

# Map of /thinking labels -> keyword injected into the prompt. Claude Code
# triggers extended thinking from keywords in the prompt; there is no numeric
# thinking API in headless mode, so this is an approximation.
THINKING_LEVELS = {
    "off": None,
    "think": "think",
    "hard": "think hard",
    "ultra": "ultrathink",
}

PROMPT_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))
TG_CHUNK = 3800  # Telegram hard limit is 4096


def read_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    raise SystemExit(
        f"No Telegram token. Set TELEGRAM_BOT_TOKEN or write it to {TOKEN_FILE}"
    )


def read_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def write_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")


# ── Claude Code agent ─────────────────────────────────────────────────────────


class ClaudeAgent:
    """Runs `claude -p` once per turn, keeping the conversation via --resume."""

    def __init__(self):
        self.session_id: str | None = None
        self.num_turns = 0
        self.last_cost = 0.0
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def new_session(self):
        self.session_id = None
        self.num_turns = 0

    def _build_args(self, prompt: str, settings: dict) -> list[str]:
        model = settings.get("model", DEFAULT_MODEL)
        args = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            model,
            "--permission-mode",
            PERMISSION_MODE,
        ]
        if BARE:
            args.append("--bare")
        if ALLOWED_TOOLS:
            tools = ",".join(ALLOWED_TOOLS.replace(",", " ").split())
            args += ["--allowedTools", tools]
        if self.session_id:
            args += ["--resume", self.session_id]
        return args

    async def prompt(self, message: str, on_tool=None) -> str:
        settings = read_settings()
        think = settings.get("thinking")
        full = message if not think else f"{message}\n\n(Please {think} about this.)"
        args = self._build_args(full, settings)

        async with self._lock:
            print(f"[bridge] running: claude -p ... (resume={self.session_id})", flush=True)
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(WORKSPACE),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        proc = self._proc
        result_text = ""
        is_error = False

        async def drain_stderr():
            assert proc.stderr
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                print(f"[claude stderr] {line.decode(errors='replace').rstrip()}",
                      file=sys.stderr, flush=True)

        err_task = asyncio.create_task(drain_stderr())

        async def read_events():
            nonlocal result_text, is_error
            assert proc.stdout
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "system" and evt.get("subtype") == "init":
                    self.session_id = evt.get("session_id") or self.session_id
                elif etype == "assistant" and on_tool:
                    content = (evt.get("message") or {}).get("content") or []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            await on_tool(block.get("name", "tool"))
                elif etype == "result":
                    sub = evt.get("subtype")
                    is_error = bool(evt.get("is_error")) or (sub not in (None, "success"))
                    result_text = evt.get("result") or result_text
                    self.session_id = evt.get("session_id") or self.session_id
                    self.last_cost = evt.get("total_cost_usd") or self.last_cost
                    self.num_turns = evt.get("num_turns") or self.num_turns

        try:
            await asyncio.wait_for(read_events(), timeout=PROMPT_TIMEOUT)
            await proc.wait()
        except asyncio.TimeoutError:
            await self.abort()
            return "⌛ Timed out waiting for Claude Code."
        finally:
            if not err_task.done():
                err_task.cancel()
            self._proc = None

        if not result_text:
            return "❌ Claude Code returned no result (check the bridge logs)." if is_error else ""
        if is_error:
            return f"⚠️ {result_text}"
        return result_text

    async def abort(self):
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass


agent = ClaudeAgent()

# ── Auth ────────────────────────────────────────────────────────────────────


def authorized(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    uid = update.effective_user.id if update.effective_user else 0
    return uid in ALLOWED_USERS


# ── Telegram helpers ──────────────────────────────────────────────────────────


async def send_long(update: Update, text: str):
    """Send plain text, split into Telegram-sized chunks. No MarkdownV2 so that
    code blocks and special characters survive untouched."""
    for i in range(0, len(text), TG_CHUNK):
        chunk = text[i : i + TG_CHUNK]
        if chunk.strip():
            await update.message.reply_text(chunk)


async def run_and_reply(update: Update, text: str):
    if agent.busy:
        await update.message.reply_text("⏳ Busy with another request. Use /abort to cancel it.")
        return

    status = await update.message.reply_text("🤔 Working...")
    last = {"label": ""}

    async def on_tool(name: str):
        label = f"🔧 {name}..."
        if label != last["label"]:
            last["label"] = label
            try:
                await status.edit_text(label)
            except Exception:
                pass

    try:
        response = await agent.prompt(text, on_tool=on_tool)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            await status.edit_text(f"❌ Error: {e}")
        except Exception:
            pass
        return

    if response.startswith("⌛"):
        try:
            await status.edit_text(response)
        except Exception:
            await update.message.reply_text(response)
        return

    try:
        await status.delete()
    except Exception:
        pass

    if not response:
        await update.message.reply_text("⚠️ Empty response.")
        return
    await send_long(update, response)


# ── Commands ──────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 Claude Code bridge — commands:\n\n"
    "/help — this message\n"
    "/new — start a fresh session\n"
    "/model — list models\n"
    "/model <alias|string> — switch model\n"
    "/thinking <off|think|hard|ultra> — reasoning hint\n"
    "/status — agent + session state\n"
    "/abort — cancel the running task\n\n"
    "Send text or a voice message to talk to Claude Code."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("🔒 Access denied.")
        return
    await update.message.reply_text(HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(HELP_TEXT)


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    settings = read_settings()
    if context.args:
        choice = " ".join(context.args).strip()
        settings["model"] = choice
        write_settings(settings)
        await update.message.reply_text(
            f"✅ Model set to '{choice}'. Applies to your next message; "
            "conversation history is preserved."
        )
        return
    current = settings.get("model", DEFAULT_MODEL)
    lines = [
        f"{'→' if alias == current else ' '} {alias} — {desc}"
        for alias, desc in KNOWN_MODELS.items()
    ]
    await update.message.reply_text(
        "Models (alias — description):\n\n"
        + "\n".join(lines)
        + f"\n\nCurrent: {current}\n\n"
        "Set with /model <alias> or a full model string (e.g. /model claude-opus-4-8)."
    )


async def thinking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    settings = read_settings()
    if not context.args:
        current = settings.get("thinking_label", "off")
        await update.message.reply_text(
            f"🧠 Thinking: {current}\n"
            "Levels: off | think | hard | ultra\n\n"
            "Note: an approximation — it injects a thinking keyword into the "
            "prompt. Claude Code has no numeric thinking API in headless mode."
        )
        return
    label = context.args[0].lower()
    if label not in THINKING_LEVELS:
        await update.message.reply_text("❌ Use: off | think | hard | ultra")
        return
    settings["thinking_label"] = label
    settings["thinking"] = THINKING_LEVELS[label]
    write_settings(settings)
    await update.message.reply_text(f"✅ Thinking set to {label}.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    settings = read_settings()
    await update.message.reply_text(
        "💻 Claude Code bridge\n"
        f"• Model: {settings.get('model', DEFAULT_MODEL)}\n"
        f"• Status: {'🔵 running' if agent.busy else '⚪ idle'}\n"
        f"• Session: {agent.session_id or 'none (fresh)'}\n"
        f"• Turns: {agent.num_turns}\n"
        f"• Last cost: ${agent.last_cost:.4f}\n"
        f"• Thinking: {settings.get('thinking_label', 'off')}\n"
        f"• Workspace: {WORKSPACE}\n"
        f"• Permission mode: {PERMISSION_MODE}"
    )


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    agent.new_session()
    await update.message.reply_text("🆕 New session started.")


async def abort_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if not agent.busy:
        await update.message.reply_text("Nothing is running.")
        return
    await agent.abort()
    await update.message.reply_text("✋ Aborted.")


# ── Message + voice handlers ───────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("🔒 Access denied.")
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await run_and_reply(update, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("🔒 Access denied.")
        return

    voice = update.message.voice
    status = await update.message.reply_text("🎤 Transcribing...")
    voice_path = f"/tmp/voice_{update.effective_user.id}.ogg"
    try:
        file = await voice.get_file()
        await file.download_to_drive(voice_path)
        segments, _info = WHISPER_MODEL.transcribe(voice_path, language=None)
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:
        import traceback
        traceback.print_exc()
        await status.edit_text(f"❌ Transcription error: {e}")
        return
    finally:
        if os.path.exists(voice_path):
            os.remove(voice_path)

    if not text:
        await status.edit_text("⚠️ Could not understand the audio.")
        return

    await status.edit_text(f"🗣️ Heard: {text}")
    await run_and_reply(update, text)


# ── Main ──────────────────────────────────────────────────────────────────────


def preflight():
    if shutil.which("claude") is None:
        raise SystemExit(
            "`claude` not found in PATH. Install Claude Code first: "
            "https://docs.claude.com/en/docs/claude-code/overview"
        )
    if BARE and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("CLAUDE_BARE is set but ANTHROPIC_API_KEY is not. Bare mode needs an API key.")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if not ALLOWED_USERS:
        print("⚠️  ALLOWED_USERS is empty: ANYONE who finds this bot can run a "
              "coding agent on this machine. Set ALLOWED_USERS.", file=sys.stderr, flush=True)
    if PERMISSION_MODE == "bypassPermissions":
        print("⚠️  PERMISSION_MODE=bypassPermissions: Claude Code will run any "
              "command without asking. Make sure WORKSPACE and ALLOWED_USERS are locked down.",
              file=sys.stderr, flush=True)


def main():
    preflight()
    token = read_token()
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("thinking", thinking_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("abort", abort_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print(f"Bridge starting. Workspace={WORKSPACE} model={DEFAULT_MODEL} "
          f"permission={PERMISSION_MODE} bare={BARE}", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
