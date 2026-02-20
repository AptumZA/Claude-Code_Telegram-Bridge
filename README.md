# Claude Code Telegram Bridge

Bidirectional Telegram bridge for [Claude Code](https://claude.com/claude-code). Sends notifications when Claude needs your attention (permissions, questions, idle) and lets you reply directly from Telegram — with replies routed to the correct Zellij session via forum topics.

## Features

- **Forum Topics** — each Claude Code session gets its own Telegram topic thread
- **Inline Buttons** — tap to answer questions or approve permissions (no typing needed)
- **Bidirectional** — send messages from Telegram, they appear in the terminal
- **Auto-registration** — sessions auto-create topics on start, close on end
- **Multi-session** — manage multiple Claude Code sessions simultaneously
- **Daemon** — lightweight background poller using Telegram long-polling

## Architecture

```
Claude Code hooks ──► notify.py ──► Telegram (notifications + buttons)
                  ──► register.py ──► sessions.json (session registry)

Telegram replies ──► daemon.py ──► zellij action write-chars ──► terminal
```

## Setup

### 1. Create a Telegram bot

- Message [@BotFather](https://t.me/BotFather) on Telegram
- Create a new bot, save the token

### 2. Create a Telegram group with forum topics

- Create a new Telegram group
- Enable **Topics/Forum** mode in group settings
- Add your bot as **admin** with "Manage Topics" permission
- Send a message in the group

### 3. Get your IDs

- **User ID**: message [@userinfobot](https://t.me/userinfobot)
- **Group Chat ID**: temporarily stop the daemon, send a message in the group, then check `getUpdates`:
  ```bash
  curl "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
  ```

### 4. Install

```bash
# Copy files to Claude Code config directory
mkdir -p ~/.claude/telegram-bridge
cp *.py config.example.json ~/.claude/telegram-bridge/

# Create config
cp ~/.claude/telegram-bridge/config.example.json ~/.claude/telegram-bridge/config.json
# Edit config.json with your bot_token, user_id, group_chat_id
chmod 600 ~/.claude/telegram-bridge/config.json

# Initialize sessions file
echo '{}' > ~/.claude/telegram-bridge/sessions.json
```

### 5. Configure Claude Code hooks

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "python3 ~/.claude/telegram-bridge/register.py", "timeout": 15 }]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [{ "type": "command", "command": "python3 ~/.claude/telegram-bridge/register.py", "timeout": 15 }]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [{ "type": "command", "command": "python3 ~/.claude/telegram-bridge/notify.py", "timeout": 15 }]
      }
    ],
    "Notification": [
      {
        "matcher": "idle_prompt",
        "hooks": [{ "type": "command", "command": "python3 ~/.claude/telegram-bridge/notify.py", "timeout": 15 }]
      }
    ],
    "Stop": [
      {
        "hooks": [{ "type": "command", "command": "python3 ~/.claude/telegram-bridge/notify.py", "timeout": 15 }]
      }
    ]
  }
}
```

### 6. Start the daemon

```bash
python3 ~/.claude/telegram-bridge/daemon.py start
```

## Usage

### From Telegram

- **Tap buttons** to answer questions or approve permissions
- **Type in a topic** to send text to that session
- `/sessions` — list active sessions
- `/help` — show usage

### Daemon management

```bash
python3 daemon.py start    # Start background daemon
python3 daemon.py stop     # Stop daemon
python3 daemon.py status   # Check if running
python3 daemon.py run      # Run in foreground (for debugging)
```

## Requirements

- Python 3.6+ (stdlib only, no external dependencies)
- [Zellij](https://zellij.dev/) terminal multiplexer
- Claude Code CLI

## How it works

### Outgoing (Claude → Telegram)

Claude pauses → hook fires → `notify.py` reads hook JSON from stdin → sends formatted message with inline buttons to the session's forum topic.

### Incoming (Telegram → Claude)

You reply in a topic or tap a button → `daemon.py` polls the update → looks up session by `topic_id` → injects text into the Zellij pane via `zellij action write-chars`.

### Selection handling

- **Defined options (1-4)**: Sends number key (`1`, `2`, `3`, `4`)
- **Built-in options (Other, Chat)**: Uses arrow key navigation
- **Permission prompts**: Sends number key matching terminal order (Yes=1, Always Allow=2, No=3)

## Security

- `config.json` is `chmod 600` (contains bot token)
- Daemon only processes messages from your `user_id`
- No remote code execution — bridge only types text into terminal panes
- File locking prevents race conditions on `sessions.json`

## License

MIT
