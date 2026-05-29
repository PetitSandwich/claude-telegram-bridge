# claude-bridge

Telegram to Claude Code bridge. Chat with [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) from Telegram, including model switching, voice messages, and multi-user access control.

Adapted from `pi-bridge`, which targeted the `pi` coding agent. This version drives Claude Code instead.

## How it works

```
Telegram user -> bot API -> bridge.py -> claude -p (stream-json) -> response back
```

Each message runs `claude -p` once in headless mode. The session id returned by Claude Code is reused with `--resume` on the next message, so the conversation keeps its history. Streaming events are parsed to show tool activity ("Running Bash...") while a task runs; the final result is sent back as a Telegram message.

## Security (read this)

This gives every allowed Telegram user a coding agent that can read, write, and run commands on the host. Lock it down:

- Always set `ALLOWED_USERS` to your own Telegram ID. Empty means anyone who finds the bot gets shell access through Claude Code.
- Point `CLAUDE_WORKSPACE` at a dedicated folder, not your home directory.
- Keep `CLAUDE_PERMISSION_MODE=acceptEdits` (the default) unless you understand the tradeoff. `bypassPermissions` lets Claude Code run any command with no approval.
- Treat anything Claude Code reads (files, web pages, repos) as untrusted input that could try to steer it. Prompt injection reaching an agent with shell access is the main risk here.

## Setup

### 1. Install Claude Code and log in

Install Claude Code and authenticate once on the host (`claude login`), or set `ANTHROPIC_API_KEY`. Confirm `claude` is on PATH.

### 2. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather), create a bot, get the token.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

`faster-whisper` is only needed for voice messages; drop it if you do not want voice.

### 4. Configure

```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, ALLOWED_USERS, CLAUDE_WORKSPACE
```

Or, instead of `TELEGRAM_BOT_TOKEN`, write the token to a file:

```bash
mkdir -p ~/.claude-bridge
echo "YOUR_BOT_TOKEN" > ~/.claude-bridge/telegram-token
```

### 5. Run

```bash
set -a; source .env; set +a
python3 bridge.py
```

## Run as a service (VPS)

```bash
sudo cp claude-bridge.service /etc/systemd/system/
# edit the paths/User in the unit and put your .env at /opt/claude-bridge/.env
sudo systemctl daemon-reload
sudo systemctl enable --now claude-bridge
journalctl -u claude-bridge -f
```

## Telegram commands

| Command | Action |
|---------|--------|
| Any text | Sent to Claude Code as a prompt |
| Voice message | Transcribed with whisper, then sent |
| `/model` | List models (opus, sonnet, haiku) |
| `/model <alias\|string>` | Switch model; history is kept |
| `/thinking <off\|think\|hard\|ultra>` | Reasoning hint (see note below) |
| `/status` | Model, session, turns, last cost, workspace |
| `/new` | Start a fresh session |
| `/abort` | Cancel the running task |

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `TELEGRAM_BOT_TOKEN` | (token file) | Bot token |
| `ALLOWED_USERS` | empty (all) | Comma-separated Telegram user IDs |
| `ANTHROPIC_API_KEY` | unset | Needed only for bare mode, or instead of `claude login` |
| `CLAUDE_WORKSPACE` | cwd | Directory Claude Code runs in |
| `CLAUDE_MODEL` | `sonnet` | Default model |
| `CLAUDE_PERMISSION_MODE` | `acceptEdits` | Permission posture |
| `CLAUDE_ALLOWED_TOOLS` | empty | Extra tools to pre-approve |
| `CLAUDE_BARE` | unset | Skip CLAUDE.md/MCP/hooks; needs API key |
| `CLAUDE_TIMEOUT` | `600` | Per-turn timeout (seconds) |
| `BRIDGE_CONFIG_DIR` | `~/.claude-bridge` | Token + settings location |

## Notes vs the pi version

- No `get_available_models` RPC exists in Claude Code headless mode, so `/model` uses aliases plus passthrough of any model string.
- No numeric thinking API in headless mode. `/thinking` is an approximation: it injects a keyword (`think` / `think hard` / `ultrathink`) into the prompt, which is what triggers extended thinking.
- `/clear` was removed. Claude Code stores transcripts under `~/.claude/projects/`; prune there if you need disk back.
- Output is sent as plain text so code blocks survive, rather than MarkdownV2.
- Heads-up on billing: per Anthropic docs, from June 15 2026 `claude -p` and Agent SDK usage on subscription plans draw from a separate monthly Agent SDK credit. Check current docs if you are on a Pro/Max plan.

## Files

- `bridge.py` - the whole bridge
- `requirements.txt` - Python deps
- `.env.example` - config template
- `claude-bridge.service` - systemd unit template
- `~/.claude-bridge/telegram-token` - optional bot token file
- `~/.claude-bridge/settings.json` - persisted model / thinking selection

## Requirements

- Python 3.10+
- Claude Code installed and on PATH, authenticated
- Telegram bot token
- `python-telegram-bot`, `faster-whisper`
