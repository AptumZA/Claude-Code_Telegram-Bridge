#!/usr/bin/env python3
"""Telegram polling daemon for Claude Code bridge.

Polls Telegram for incoming messages from forum topics, maps topic_id
to session, and injects text into the correct tmux session.

Usage:
    python3 daemon.py start     # Start as background daemon
    python3 daemon.py stop      # Stop the daemon
    python3 daemon.py status    # Check if running
    python3 daemon.py run       # Run in foreground (for testing)
"""

import json
import os
import sys
import signal
import time
import glob
import subprocess
import urllib.request
import urllib.error
import fcntl
import logging
from logging.handlers import RotatingFileHandler

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")
SESSIONS_FILE = os.path.join(BRIDGE_DIR, "sessions.json")
BUSY_DIR = os.path.join(BRIDGE_DIR, "busy")


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


CONFIG = load_config()
PID_FILE = CONFIG.get("pid_file", os.path.join(BRIDGE_DIR, "daemon.pid"))
LOG_FILE = CONFIG.get("log_file", os.path.join(BRIDGE_DIR, "bridge.log"))
POLL_TIMEOUT = CONFIG.get("poll_interval", 30)
GROUP_CHAT_ID = CONFIG.get("group_chat_id")
USER_ID = CONFIG.get("user_id")

# Set up logging
logger = logging.getLogger("telegram-bridge")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)


def set_busy(session_name, message_id):
    """Mark a session as busy with the message_id to react to."""
    os.makedirs(BUSY_DIR, exist_ok=True)
    path = os.path.join(BUSY_DIR, session_name)
    with open(path, "w") as f:
        f.write(str(message_id))


def react_to_message(message_id, emoji):
    """Set a reaction emoji on a message."""
    try:
        result = telegram_api("setMessageReaction", {
            "chat_id": GROUP_CHAT_ID,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        })
        logger.info(f"Reacted to msg {message_id} with {emoji}: {result.get('ok')}")
        return True
    except Exception as e:
        logger.error(f"setMessageReaction failed for msg {message_id} emoji={emoji}: {e}")
        return False


def load_sessions():
    """Load sessions.json with shared lock."""
    try:
        with open(SESSIONS_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sessions(sessions):
    """Save sessions.json with file locking."""
    with open(SESSIONS_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(sessions, f, indent=2)
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)


def find_session_by_topic(topic_id):
    """Find session name and info by forum topic_id."""
    sessions = load_sessions()
    for name, info in sessions.items():
        if info.get("topic_id") == topic_id:
            return name, info
    return None, None


def telegram_api(method, params=None):
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{CONFIG['bot_token']}/{method}"
    if params:
        data = json.dumps(params).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10)
    return json.loads(resp.read().decode())


def send_to_topic(topic_id, text, parse_mode="HTML"):
    """Send a message to a specific forum topic."""
    try:
        params = {
            "chat_id": GROUP_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        }
        if topic_id:
            params["message_thread_id"] = topic_id
        telegram_api("sendMessage", params)
    except Exception as e:
        logger.error(f"Failed to send to topic {topic_id}: {e}")


def send_to_general(text, parse_mode="HTML"):
    """Send a message to the General topic (no thread_id)."""
    try:
        telegram_api("sendMessage", {
            "chat_id": GROUP_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        })
    except Exception as e:
        logger.error(f"Failed to send to general: {e}")


def is_session_alive(session_name):
    """Check if a tmux session exists and is running."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def cwd_to_project_dir(cwd):
    """Convert a working directory to Claude's project session directory path.

    Claude encodes paths by replacing / and _ with -, e.g.:
    /home/admin1/aptum/white_labeling -> -home-admin1-aptum-white-labeling
    """
    encoded = cwd.replace("/", "-").replace("_", "-")
    return os.path.expanduser(f"~/.claude/projects/{encoded}")


def list_claude_sessions(cwd):
    """List available Claude Code sessions for a given working directory.

    Returns list of dicts: {id, name, first_msg, mtime, age}
    """
    project_dir = cwd_to_project_dir(cwd)
    sessions = []
    now = time.time()
    for f in glob.glob(os.path.join(project_dir, "*.jsonl")):
        sid = os.path.basename(f).replace(".jsonl", "")
        mtime = os.path.getmtime(f)
        name = None
        first_msg = None
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "custom-title":
                            name = entry.get("customTitle", "")
                        if first_msg is None and entry.get("type") == "user":
                            # Skip tool result messages
                            if entry.get("toolUseResult"):
                                continue
                            msg = entry.get("message", {})
                            content = msg.get("content", []) if isinstance(msg, dict) else []
                            if isinstance(content, str):
                                text = content.strip()
                                if text and not text.startswith("[Request"):
                                    first_msg = text[:60]
                            elif isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        text = c["text"].strip()
                                        if text and not text.startswith("[Request"):
                                            first_msg = text[:60]
                                            break
                    except (json.JSONDecodeError, KeyError):
                        pass
        except OSError:
            pass
        # Human-readable age
        age_secs = now - mtime
        if age_secs < 3600:
            age = f"{int(age_secs / 60)}m"
        elif age_secs < 86400:
            age = f"{int(age_secs / 3600)}h"
        else:
            age = f"{int(age_secs / 86400)}d"
        # File size
        try:
            size_bytes = os.path.getsize(f)
            if size_bytes < 1024:
                size = f"{size_bytes}B"
            elif size_bytes < 1024 * 1024:
                size = f"{size_bytes // 1024}KB"
            else:
                size = f"{size_bytes // (1024 * 1024)}MB"
        except OSError:
            size = "?"
        sessions.append({
            "id": sid,
            "name": name,
            "first_msg": first_msg,
            "mtime": mtime,
            "age": age,
            "size": size,
        })
    # Sort by most recently modified first
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def start_tmux_with_claude(tmux_name, cwd, claude_args=""):
    """Start a new tmux session running Claude Code.

    Args:
        tmux_name: tmux session name
        cwd: working directory for the session
        claude_args: extra args for claude command (e.g. '--resume name')
    """
    cmd = f"claude {claude_args}".strip()
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-c", cwd, cmd],
            timeout=10, capture_output=True,
        )
        # Give Claude a moment to start
        time.sleep(1)
        return is_session_alive(tmux_name)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error(f"Failed to start tmux session {tmux_name}: {e}")
        return False


def inject_into_session(session_name, text):
    """Inject text into a tmux session via send-keys + Enter."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, text],
            timeout=5, capture_output=True,
        )
        time.sleep(0.1)
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            timeout=5, capture_output=True,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error(f"tmux injection failed for {session_name}: {e}")
        return False


def inject_selection_into_session(session_name, index, num_defined_options=4):
    """Select an option in AskUserQuestion UI.

    Defined options (index < num_defined_options) use number keys.
    Built-in options (Other, Chat) use arrow navigation.
    """
    try:
        if index < num_defined_options:
            number = str(index + 1)
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, number],
                timeout=5, capture_output=True,
            )
        else:
            for _ in range(index):
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name, "Down"],
                    timeout=5, capture_output=True,
                )
                time.sleep(0.05)
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                timeout=5, capture_output=True,
            )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error(f"tmux selection failed for {session_name}: {e}")
        return False


def inject_permission_into_session(session_name, choice):
    """Handle permission prompt selection using number keys.

    Permission prompt order: 1=Yes, 2=Always Allow, 3=No
    """
    perm_map = {"yes": "1", "always": "2", "no": "3"}
    number = perm_map.get(choice)
    if not number:
        return False

    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, number],
            timeout=5, capture_output=True,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error(f"tmux permission failed for {session_name}: {e}")
        return False


def handle_sessions_command(topic_id=None):
    """Handle /tel_sessions command — list tmux sessions."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        tmux_sessions = [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        tmux_sessions = []

    if not tmux_sessions:
        send_to_topic(topic_id, "No tmux sessions found.")
        return

    bridge_sessions = load_sessions()

    lines = ["<b>Sessions:</b>"]
    for ts in tmux_sessions:
        info = bridge_sessions.get(ts, {})
        active = info.get("active", False)
        has_topic = "\u2705" if info.get("topic_id") else "\u2796"
        cwd = info.get("cwd", "")
        status = "\U0001F7E2" if active else "\u26aa"
        line = f"\n{status} <b>{ts}</b> {has_topic}"
        if cwd:
            line += f"\n  <i>{cwd}</i>"
        lines.append(line)

    lines.append(f"\n\U0001F7E2 = bridge active, \u26aa = no bridge")
    lines.append(f"\u2705 = has topic, \u2796 = no topic")
    send_to_topic(topic_id, "\n".join(lines))


def get_topic_display_name(session_name, backend):
    """Get the topic display name with backend prefix."""
    prefix_map = {"tmux": "tmux_", "zellij": "zell_"}
    prefix = prefix_map.get(backend, "")
    return f"{prefix}{session_name}"


def handle_rename_command(topic_id, args_text):
    """Handle /tel_rename command — rename a session."""
    new_name = args_text.strip()
    if not new_name:
        send_to_topic(topic_id, "\u26a0\ufe0f Usage: <code>/tel_rename new_name</code>")
        return

    session_name, session_info = find_session_by_topic(topic_id)
    if not session_name:
        send_to_topic(topic_id, "\u26a0\ufe0f No session linked to this topic.")
        return

    tmux_session = session_info.get("tmux_session") or session_info.get("zellij_session", "")
    if not tmux_session:
        send_to_topic(topic_id, "\u26a0\ufe0f Session has no terminal session.")
        return

    # Update the bridge mapping and rename the Telegram topic
    sessions = load_sessions()

    # Update sessions.json: move entry to new name
    old_info = sessions.pop(session_name, {})
    old_info["tmux_session"] = tmux_session
    sessions[new_name] = old_info
    save_sessions(sessions)

    # Rename the Telegram forum topic with backend prefix
    backend = old_info.get("backend", "tmux")
    topic_display = get_topic_display_name(new_name, backend)
    try:
        telegram_api("editForumTopic", {
            "chat_id": GROUP_CHAT_ID,
            "message_thread_id": topic_id,
            "name": topic_display,
        })
    except Exception as e:
        logger.error(f"Failed to rename topic: {e}")

    send_to_topic(topic_id, f"\u2705 Renamed: <b>{session_name}</b> \u2192 <b>{new_name}</b>")
    logger.info(f"Renamed session {session_name} -> {new_name}")


def handle_session_start(topic_id):
    """Handle /tel_session_start — start tmux session and offer Claude sessions to resume."""
    session_name, session_info = find_session_by_topic(topic_id)
    if not session_name:
        send_to_topic(topic_id, "\u26a0\ufe0f No session linked to this topic.")
        return

    tmux_name = session_info.get("tmux_session") or session_name
    if is_session_alive(tmux_name):
        send_to_topic(topic_id, f"\u2705 <b>{tmux_name}</b> is already running.")
        return

    cwd = session_info.get("cwd", os.path.expanduser("~"))
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")

    # List available Claude sessions for this directory
    claude_sessions = list_claude_sessions(cwd)

    if not claude_sessions:
        # No existing sessions — just start fresh
        if start_tmux_with_claude(tmux_name, cwd):
            send_to_topic(topic_id, f"\u2705 Started <b>{tmux_name}</b> with new Claude session\n<i>{cwd}</i>")
        else:
            send_to_topic(topic_id, f"\u274c Failed to start <b>{tmux_name}</b>")
        return

    # Build inline keyboard with session choices
    buttons = []
    for i, cs in enumerate(claude_sessions[:8]):  # max 8 sessions
        # Prefer name, fall back to first message, then truncated ID
        if cs["name"]:
            label = cs["name"]
        elif cs["first_msg"]:
            label = cs["first_msg"]
        else:
            label = cs["id"][:8]
        # Truncate and add age
        if len(label) > 25:
            label = label[:22] + "..."
        label = f"{label} ({cs['age']}, {cs['size']})"
        cb_data = f"{session_name}|start|resume|{cs['id']}"
        buttons.append([{"text": f"\U0001F504 {label}", "callback_data": cb_data}])

    # Add "New session" and "Delete sessions" options
    buttons.append([
        {"text": "\u2795 New session", "callback_data": f"{session_name}|start|new|_"},
        {"text": "\U0001F5D1 Delete", "callback_data": f"{session_name}|start|delete_menu|_"},
    ])

    try:
        telegram_api("sendMessage", {
            "chat_id": GROUP_CHAT_ID,
            "message_thread_id": topic_id,
            "text": f"\U0001F4C2 <b>{tmux_name}</b>\n<i>{cwd}</i>\n\nSelect a Claude session to resume:",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        })
    except Exception as e:
        logger.error(f"Failed to send session picker: {e}")
        # Fallback: just start with continue
        if start_tmux_with_claude(tmux_name, cwd, "-c"):
            send_to_topic(topic_id, f"\u2705 Started <b>{tmux_name}</b> (continued last session)")
        else:
            send_to_topic(topic_id, f"\u274c Failed to start <b>{tmux_name}</b>")


def handle_session_end(topic_id):
    """Handle /tel_session_end — kill the tmux session."""
    session_name, session_info = find_session_by_topic(topic_id)
    if not session_name:
        send_to_topic(topic_id, "\u26a0\ufe0f No session linked to this topic.")
        return

    tmux_name = session_info.get("tmux_session") or session_name
    if not is_session_alive(tmux_name):
        send_to_topic(topic_id, f"\u26aa <b>{tmux_name}</b> is not running.")
        return

    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", tmux_name],
            timeout=5, capture_output=True,
        )
        send_to_topic(topic_id, f"\u274c Stopped <b>{tmux_name}</b>")
        logger.info(f"Killed tmux session {tmux_name}")
    except Exception as e:
        logger.error(f"Failed to kill tmux session {tmux_name}: {e}")
        send_to_topic(topic_id, f"\u274c Failed to stop <b>{tmux_name}</b>")


def handle_help_command(topic_id=None):
    """Handle /tel_help command."""
    send_to_topic(topic_id,
        "<b>Telegram-Claude Bridge</b>\n\n"
        "<b>Forum Topics Mode:</b>\n"
        "Each session has its own topic. Just type your reply in the topic \u2014 no prefix needed.\n\n"
        "<b>Bridge Commands (tel_):</b>\n"
        "/tel_sessions - List sessions\n"
        "/tel_session_start - Start tmux + Claude session\n"
        "/tel_session_end - Stop tmux session\n"
        "/tel_rename &lt;name&gt; - Rename session/topic\n"
        "/tel_help - Show this help\n\n"
        "<b>Claude Code Commands:</b>\n"
        "All other /<i>command</i> entries in the menu are forwarded to the Claude Code session "
        "linked to this topic (e.g. /compact, /init, /model)."
    )


# Claude Code slash commands that get forwarded to the Zellij session
CLAUDE_COMMANDS = {
    "clear", "compact", "config", "context", "cost", "debug", "doctor",
    "exit", "export", "init", "mcp", "memory", "model", "permissions",
    "plan", "rename", "resume", "rewind", "stats", "status", "statusline",
    "copy", "tasks", "theme", "todos", "usage", "vim",
}


def is_authorized(message):
    """Check if message is from the authorized user in the group."""
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    # Accept messages from the group, sent by the authorized user
    if chat_id == GROUP_CHAT_ID and user_id == USER_ID:
        return True
    return False


def process_message(message):
    """Process a single Telegram message from a forum topic."""
    if not is_authorized(message):
        logger.warning(f"Ignoring unauthorized message from chat={message.get('chat',{}).get('id')} user={message.get('from',{}).get('id')}")
        return

    text = message.get("text", "")
    topic_id = message.get("message_thread_id")

    if not text:
        return

    logger.info(f"Received in topic {topic_id}: {text}")

    # Handle bridge commands (tel_ prefixed)
    if text.startswith("/tel_sessions"):
        handle_sessions_command(topic_id)
        return
    if text.startswith("/tel_session_start"):
        handle_session_start(topic_id)
        return
    if text.startswith("/tel_session_end"):
        handle_session_end(topic_id)
        return
    if text.startswith("/tel_rename"):
        handle_rename_command(topic_id, text[len("/tel_rename"):])
        return
    if text.startswith("/tel_help") or text.startswith("/start"):
        handle_help_command(topic_id)
        return

    # Check if this is a Claude Code slash command to forward
    cmd_word = text.split()[0].lstrip("/").split("@")[0] if text.startswith("/") else None
    is_claude_cmd = cmd_word in CLAUDE_COMMANDS if cmd_word else False

    # Find session by topic
    session_name, session_info = find_session_by_topic(topic_id)

    if not session_name:
        send_to_topic(topic_id, "\u26a0\ufe0f No session linked to this topic.")
        return

    if not session_info.get("active", True):
        send_to_topic(topic_id, f"\u26a0\ufe0f Session <b>{session_name}</b> is not active.")
        return

    tmux_session = session_info.get("tmux_session") or session_info.get("zellij_session", "")
    if not tmux_session:
        send_to_topic(topic_id, f"\u26a0\ufe0f Session <b>{session_name}</b> has no tmux session.")
        return

    if not is_session_alive(tmux_session):
        # Offer to start the session
        send_to_topic(topic_id,
            f"\u26a0\ufe0f <b>{tmux_session}</b> is not running.\n"
            f"Use /tel_session_start to start it.")
        return

    msg_id = message.get("message_id")

    # Claude Code slash commands: forward as-is (no [Telegram] prefix)
    if is_claude_cmd:
        slash_cmd = f"/{cmd_word}"
        if inject_into_session(tmux_session, slash_cmd):
            react_to_message(msg_id, "\U0001F44D")  # Received
            set_busy(session_name, msg_id)
            react_to_message(msg_id, "\U0001F440")  # Busy
            logger.info(f"Claude command injected into {tmux_session}: {slash_cmd}")
        else:
            send_to_topic(topic_id, f"\u274c Failed to send.")
        return

    # Inject with [Telegram] prefix
    prefixed_text = f"[Telegram] {text}"
    if inject_into_session(tmux_session, prefixed_text):
        react_to_message(msg_id, "\U0001F44D")  # Received
        set_busy(session_name, msg_id)
        react_to_message(msg_id, "\U0001F440")  # Busy
        logger.info(f"Injected into {tmux_session}: {prefixed_text}")
    else:
        send_to_topic(topic_id, f"\u274c Failed to send.")


def process_callback_query(callback_query):
    """Process an inline keyboard button tap.

    Callback data formats:
        session_name|opt|INDEX|NUM_DEFINED - Select option in AskUserQuestion UI
        session_name|perm|INDEX            - Select permission option INDEX
    """
    cb_id = callback_query.get("id", "")
    cb_data = callback_query.get("data", "")
    user_id = callback_query.get("from", {}).get("id")

    if user_id != USER_ID:
        logger.warning(f"Ignoring callback from unauthorized user: {user_id}")
        return

    logger.info(f"Callback: {cb_data}")

    parts = cb_data.split("|")
    if len(parts) < 2:
        telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Invalid button data"})
        return

    session_name = parts[0]
    sessions = load_sessions()

    if session_name not in sessions:
        telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Session {session_name} not found"})
        return

    session_info = sessions[session_name]
    tmux_session = session_info.get("tmux_session") or session_info.get("zellij_session", "")
    topic_id = session_info.get("topic_id")

    if not tmux_session:
        telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "No tmux session"})
        return

    action_type = parts[1] if len(parts) >= 3 else "text"
    action_value = parts[2] if len(parts) >= 3 else parts[1]

    # Get button label for confirmation
    button_text = action_value
    for row in (callback_query.get("message", {}).get("reply_markup", {}).get("inline_keyboard", [])):
        for btn in row:
            if btn.get("callback_data") == cb_data:
                button_text = btn.get("text", action_value)
                break

    # Get the bot's message_id (the message with inline buttons)
    cb_msg_id = callback_query.get("message", {}).get("message_id")

    if action_type == "start":
        claude_session_id = parts[3] if len(parts) >= 4 else "_"
        cwd = session_info.get("cwd", os.path.expanduser("~"))
        if not cwd or not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")

        if action_value == "delete_menu":
            # Show delete picker
            claude_sessions = list_claude_sessions(cwd)
            if not claude_sessions:
                telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "No sessions to delete"})
                return
            buttons = []
            for cs in claude_sessions[:8]:
                if cs["name"]:
                    label = cs["name"]
                elif cs["first_msg"]:
                    label = cs["first_msg"]
                else:
                    label = cs["id"][:8]
                if len(label) > 22:
                    label = label[:19] + "..."
                label = f"{label} ({cs['age']}, {cs['size']})"
                buttons.append([{"text": f"\U0001F5D1 {label}", "callback_data": f"{session_name}|start|delete|{cs['id']}"}])
            buttons.append([{"text": "\u2b05 Back", "callback_data": f"{session_name}|start|back|_"}])
            # Edit the existing message to show delete picker
            cb_msg_id = callback_query.get("message", {}).get("message_id")
            if cb_msg_id:
                try:
                    telegram_api("editMessageText", {
                        "chat_id": GROUP_CHAT_ID,
                        "message_id": cb_msg_id,
                        "text": f"\U0001F5D1 <b>Delete a Claude session:</b>\n<i>{cwd}</i>",
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": buttons},
                    })
                except Exception as e:
                    logger.error(f"Failed to edit message for delete menu: {e}")
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Select session to delete"})
            return

        if action_value == "delete" and claude_session_id != "_":
            # Delete the JSONL session file
            project_dir = cwd_to_project_dir(cwd)
            session_file = os.path.join(project_dir, f"{claude_session_id}.jsonl")
            try:
                os.remove(session_file)
                telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Session deleted"})
                send_to_topic(topic_id, f"\U0001F5D1 Deleted session <code>{claude_session_id[:8]}</code>")
                logger.info(f"Deleted Claude session file: {session_file}")
            except FileNotFoundError:
                telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Session not found"})
            except OSError as e:
                telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Delete failed: {e}"})
            return

        if action_value == "back":
            # Go back to the start menu — re-trigger session start
            handle_session_start(topic_id)
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": ""})
            return

        if action_value == "resume" and claude_session_id != "_":
            claude_args = f"--resume {claude_session_id}"
            label = "Resuming session"
        else:
            claude_args = ""
            label = "New session"

        telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"{label}..."})

        if start_tmux_with_claude(tmux_session, cwd, claude_args):
            send_to_topic(topic_id, f"\u2705 Started <b>{tmux_session}</b>\n{label}")
            logger.info(f"Started tmux {tmux_session} in {cwd} with: claude {claude_args}")
        else:
            send_to_topic(topic_id, f"\u274c Failed to start <b>{tmux_session}</b>")
        return

    if action_type == "perm":
        # Permission prompt: use Y/N keys or arrow navigation
        if inject_permission_into_session(tmux_session, action_value):
            if cb_msg_id:
                set_busy(session_name, cb_msg_id)
                react_to_message(cb_msg_id, "\U0001F440")
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Selected: {button_text[:50]}"})
            send_to_topic(topic_id, f"\u2705 Selected: <code>{button_text[:100]}</code>")
            logger.info(f"Permission '{action_value}' injected into {tmux_session}")
        else:
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Failed to send"})
            send_to_topic(topic_id, f"\u274c Failed to send.")
    elif action_type == "opt":
        # AskUserQuestion: number keys for defined options, arrows for built-in
        try:
            index = int(action_value)
        except ValueError:
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Invalid index"})
            return

        num_defined = int(parts[3]) if len(parts) >= 4 else 99

        if inject_selection_into_session(tmux_session, index, num_defined):
            if cb_msg_id:
                set_busy(session_name, cb_msg_id)
                react_to_message(cb_msg_id, "\U0001F440")
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Selected: {button_text[:50]}"})
            send_to_topic(topic_id, f"\u2705 Selected: <code>{button_text[:100]}</code>")
            logger.info(f"Selection {index} injected into {tmux_session}: {button_text}")
        else:
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Failed to send"})
            send_to_topic(topic_id, f"\u274c Failed to send.")
    else:
        if inject_into_session(tmux_session, action_value):
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Sent: {action_value[:50]}"})
            send_to_topic(topic_id, f"\u2705 <code>{action_value[:100]}</code>")
            logger.info(f"Button tap injected into {tmux_session}: {action_value}")
        else:
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Failed to send"})
            send_to_topic(topic_id, f"\u274c Failed to send.")


def poll_loop():
    """Main polling loop using Telegram long-polling."""
    offset = None
    logger.info("Daemon started, entering poll loop")

    while True:
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset

            result = telegram_api("getUpdates", params)

            if result.get("ok") and result.get("result"):
                for update in result["result"]:
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        try:
                            process_callback_query(update["callback_query"])
                        except Exception as e:
                            logger.error(f"Error processing callback: {e}")
                    elif "message" in update:
                        try:
                            process_message(update["message"])
                        except Exception as e:
                            logger.error(f"Error processing message: {e}")

        except urllib.error.URLError as e:
            logger.warning(f"Network error: {e}. Retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Poll error: {e}. Retrying in 5s...")
            time.sleep(5)


def write_pid():
    """Write PID file."""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    """Remove PID file."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def get_pid():
    """Read PID from file and check if process is alive."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def handle_signal(signum, frame):
    """Handle SIGTERM/SIGINT gracefully."""
    logger.info(f"Received signal {signum}, shutting down")
    remove_pid()
    sys.exit(0)


def cmd_start():
    """Start daemon in background."""
    pid = get_pid()
    if pid:
        print(f"Daemon already running (PID {pid})")
        return

    if os.fork() > 0:
        print("Daemon starting...")
        return

    os.setsid()

    if os.fork() > 0:
        os._exit(0)

    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    write_pid()
    logger.info(f"Daemon started (PID {os.getpid()})")

    try:
        poll_loop()
    except Exception as e:
        logger.error(f"Daemon crashed: {e}")
    finally:
        remove_pid()


def cmd_stop():
    """Stop daemon."""
    pid = get_pid()
    if not pid:
        print("Daemon is not running.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except ProcessLookupError:
                break
        print(f"Daemon stopped (was PID {pid})")
    except ProcessLookupError:
        print("Daemon was not running.")
    finally:
        remove_pid()


def cmd_status():
    """Check daemon status."""
    pid = get_pid()
    if pid:
        print(f"Daemon is running (PID {pid})")
    else:
        print("Daemon is not running.")


def cmd_run():
    """Run in foreground (for testing)."""
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    write_pid()
    print(f"Running in foreground (PID {os.getpid()}). Ctrl+C to stop.")
    logger.info(f"Running in foreground (PID {os.getpid()})")

    try:
        poll_loop()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        remove_pid()


def main():
    if len(sys.argv) < 2:
        print("Usage: daemon.py {start|stop|status|run}")
        sys.exit(1)

    cmd = sys.argv[1]
    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "run": cmd_run,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: daemon.py {start|stop|status|run}")
        sys.exit(1)


if __name__ == "__main__":
    main()
