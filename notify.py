#!/usr/bin/env python3
"""Notification sender for Claude Code hooks.

Handles: Notification, Stop, PermissionRequest.
Reads hook JSON from stdin, sends contextual Telegram messages with
inline keyboard buttons to the correct forum topic per session.
Fire-and-forget: catches all exceptions, always exits 0.
"""

import json
import os
import sys
import fcntl
import subprocess
import urllib.request
import urllib.error
import logging
from logging.handlers import RotatingFileHandler

import time

# Logging for debugging
BRIDGE_DIR_LOG = os.path.dirname(os.path.abspath(__file__))
_logger = logging.getLogger("notify")
_logger.setLevel(logging.DEBUG)
_log_handler = RotatingFileHandler(os.path.join(BRIDGE_DIR_LOG, "notify.log"), maxBytes=500_000, backupCount=2)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_logger.addHandler(_log_handler)

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")
SESSIONS_FILE = os.path.join(BRIDGE_DIR, "sessions.json")
PENDING_DIR = os.path.join(BRIDGE_DIR, "pending")
BUSY_DIR = os.path.join(BRIDGE_DIR, "busy")


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_session_name():
    """Derive session name from tmux or Zellij env vars."""
    # Try tmux first
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    # Fallback to Zellij
    zellij_name = os.environ.get("ZELLIJ_SESSION_NAME", "")
    if zellij_name:
        return zellij_name
    return "unknown"


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


def get_topic_id(session_name):
    """Get the forum topic_id for a session."""
    sessions = load_sessions()
    session = sessions.get(session_name, {})
    return session.get("topic_id")


def html_escape(text):
    """Escape HTML special chars for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(config, text, reply_markup=None, topic_id=None):
    """Send a Telegram message to the group forum topic."""
    url = f"https://api.telegram.org/bot{config['bot_token']}/sendMessage"
    payload = {
        "chat_id": config["group_chat_id"],
        "text": text,
        "parse_mode": "HTML",
    }
    if topic_id:
        payload["message_thread_id"] = topic_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def extract_last_assistant_message(transcript_path):
    """Extract the last assistant text message from a JSONL transcript file.

    Reads the file backwards (last 50 lines) to find the most recent
    assistant message with text content.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""

    try:
        # Read last portion of file to find recent assistant messages
        with open(transcript_path, "rb") as f:
            # Seek to end, read last ~100KB
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 100_000)
            f.seek(size - read_size)
            tail = f.read().decode("utf-8", errors="replace")

        lines = tail.strip().splitlines()
        # Search backwards for the last assistant message with text
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Direct assistant message
            msg = None
            if entry.get("type") == "assistant":
                msg = entry.get("message", {})
            elif entry.get("type") == "progress":
                inner = entry.get("data", {}).get("message", {})
                if inner.get("type") == "assistant":
                    msg = inner.get("message", {})

            if not msg:
                continue

            content = msg.get("content", [])
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

            if text_parts:
                return "\n".join(text_parts)

    except Exception as e:
        _logger.error(f"Failed to read transcript: {e}")

    return ""


def clear_busy(session_name, config):
    """Clear busy marker and react with checkmark."""
    path = os.path.join(BUSY_DIR, session_name)
    try:
        with open(path) as f:
            message_id = int(f.read().strip())
        os.remove(path)
        # React with ✅ to signal completion
        url = f"https://api.telegram.org/bot{config['bot_token']}/setMessageReaction"
        payload = {
            "chat_id": config["group_chat_id"],
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": "\U0001F3C6"}],
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except FileNotFoundError:
        pass
    except Exception as e:
        _logger.error(f"clear_busy error: {e}")


def set_pending_permission(session_name):
    """Mark that a permission prompt is active for this session."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    path = os.path.join(PENDING_DIR, session_name)
    with open(path, "w") as f:
        f.write(str(time.time()))


def consume_pending_permission(session_name):
    """Check and clear pending permission marker. Returns True if one existed."""
    path = os.path.join(PENDING_DIR, session_name)
    try:
        with open(path) as f:
            ts = float(f.read().strip())
        os.remove(path)
        # Only valid if less than 5 minutes old
        return (time.time() - ts) < 300
    except (FileNotFoundError, ValueError):
        return False


def build_ask_question_message(hook_input, session_name):
    """Build message text and inline keyboard for AskUserQuestion."""
    tool_input = hook_input.get("tool_input", {})
    questions = tool_input.get("questions", [])

    if not questions:
        return f"\u2753 Question (no details)", None

    lines = [f"\u2753 <b>Question for you</b>"]
    keyboard_rows = []

    for q in questions:
        question_text = q.get("question", "")
        options = q.get("options", [])
        multi = q.get("multiSelect", False)

        lines.append(f"\n<b>{html_escape(question_text)}</b>")
        if multi:
            lines.append("<i>(multiple selections allowed)</i>")

        for j, opt in enumerate(options):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            if desc:
                lines.append(f"  \u2022 <b>{html_escape(label)}</b> \u2014 <i>{html_escape(desc)}</i>")
            else:
                lines.append(f"  \u2022 <b>{html_escape(label)}</b>")

            # Callback data: session|opt|INDEX|NUM_DEFINED (0-based index)
            cb_data = f"{session_name}|opt|{j}|{len(options)}"
            keyboard_rows.append([{"text": label, "callback_data": cb_data}])

        # Claude Code adds built-in options after the defined ones:
        # N+1 = "Other" (type custom text)
        # N+2 = "Let's chat about it" (discuss the question)
        num_opts = len(options)
        keyboard_rows.append([
            {"text": "\u270f\ufe0f Other", "callback_data": f"{session_name}|opt|{num_opts}|{num_opts}"},
            {"text": "\U0001F4AC Chat about it", "callback_data": f"{session_name}|opt|{num_opts + 1}|{num_opts}"},
        ])

    lines.append(f"\n<i>Or type a custom answer below</i>")

    reply_markup = {"inline_keyboard": keyboard_rows}
    return "\n".join(lines), reply_markup


def build_permission_message(hook_input, session_name):
    """Build message text and inline keyboard for permission requests."""
    tool_name = hook_input.get("tool_name", "unknown")
    tool_input = hook_input.get("tool_input", {})

    # AskUserQuestion gets its own rich format
    if tool_name == "AskUserQuestion":
        return build_ask_question_message(hook_input, session_name)

    lines = [f"\U0001F510 <b>Permission needed</b>"]
    lines.append(f"\nTool: <b>{html_escape(tool_name)}</b>")

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            lines.append(f"<i>{html_escape(desc)}</i>")
        if cmd:
            cmd_display = cmd if len(cmd) <= 300 else cmd[:297] + "..."
            lines.append(f"<code>{html_escape(cmd_display)}</code>")
    elif tool_name in ("Write", "Edit", "Read"):
        fp = tool_input.get("file_path", "")
        if fp:
            lines.append(f"File: <code>{html_escape(fp)}</code>")
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "")
        if url:
            lines.append(f"URL: <code>{html_escape(url)}</code>")
    else:
        details = json.dumps(tool_input, indent=2)
        if len(details) > 300:
            details = details[:297] + "..."
        lines.append(f"<code>{html_escape(details)}</code>")

    # Permission buttons using named actions (Y key, N key, arrow+enter)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "\u2705 Yes", "callback_data": f"{session_name}|perm|yes"},
                {"text": "\U0001F513 Always allow", "callback_data": f"{session_name}|perm|always"},
            ],
            [
                {"text": "\u274c No", "callback_data": f"{session_name}|perm|no"},
            ],
        ]
    }

    return "\n".join(lines), keyboard


def format_post_tool(hook_input, session_name):
    """Format PostToolUse confirmation — tool ran successfully (permission granted)."""
    tool_name = hook_input.get("tool_name", "unknown")
    tool_input = hook_input.get("tool_input", {})

    if tool_name == "AskUserQuestion":
        return None, None  # Don't double-notify for questions

    summary = f"\u2705 <b>Allowed</b>: {html_escape(tool_name)}"

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            summary += f"\n<i>{html_escape(desc)}</i>"
        elif cmd:
            cmd_short = cmd if len(cmd) <= 100 else cmd[:97] + "..."
            summary += f"\n<code>{html_escape(cmd_short)}</code>"
    elif tool_name in ("Write", "Edit"):
        fp = tool_input.get("file_path", "")
        if fp:
            summary += f"\n<code>{html_escape(fp)}</code>"

    return summary, None


def format_post_tool_failure(hook_input, session_name):
    """Format PostToolUseFailure — tool was denied or failed."""
    tool_name = hook_input.get("tool_name", "unknown")
    error = hook_input.get("error", "")

    summary = f"\u274c <b>Denied/Failed</b>: {html_escape(tool_name)}"
    if error:
        error_short = error if len(error) <= 200 else error[:197] + "..."
        summary += f"\n<i>{html_escape(error_short)}</i>"

    return summary, None


def format_notification(hook_input, session_name):
    """Format a notification message. Returns (text, reply_markup) tuple."""
    event = hook_input.get("hook_event_name", "")

    if event == "PermissionRequest":
        set_pending_permission(session_name)
        return build_permission_message(hook_input, session_name)

    if event == "PostToolUse":
        # Only notify if there was a recent permission prompt (not auto-allowed)
        if consume_pending_permission(session_name):
            return format_post_tool(hook_input, session_name)
        return None, None

    if event == "PostToolUseFailure":
        # Always notify on failure if there was a pending permission
        if consume_pending_permission(session_name):
            return format_post_tool_failure(hook_input, session_name)
        return None, None

    if event == "Notification":
        notif_type = hook_input.get("notification_type", "")
        message = hook_input.get("message", "")
        title = hook_input.get("title", "")

        emoji_map = {
            "permission_prompt": "\U0001F510",
            "idle_prompt": "\U0001F4A4",
            "elicitation_dialog": "\u2753",
            "auth_success": "\U0001F511",
            "compact": "\U0001F4E6",
            "context_compaction": "\U0001F4E6",
            "compacting": "\U0001F4E6",
        }
        emoji = emoji_map.get(notif_type, "\U0001F514")

        label_map = {
            "permission_prompt": "Permission needed",
            "idle_prompt": "Idle / waiting for input",
            "elicitation_dialog": "Question for you",
            "auth_success": "Auth success",
            "compact": "Compacting context",
            "context_compaction": "Compacting context",
            "compacting": "Compacting context",
        }
        label = label_map.get(notif_type, notif_type or "Notification")

        text = f"{emoji} <b>{label}</b>"
        if title and title != label:
            text += f"\n<b>{html_escape(title)}</b>"
        if message:
            if len(message) > 300:
                message = message[:297] + "..."
            text += f"\n<i>{html_escape(message)}</i>"
        return text, None

    if event == "Stop":
        stop_active = hook_input.get("stop_hook_active", False)
        transcript_path = hook_input.get("transcript_path", "")

        # Extract last assistant message from transcript
        last_msg = extract_last_assistant_message(transcript_path)

        header = f"\U0001F6D1 <b>Stopped</b>"
        if stop_active:
            header += " (may need input)"

        if last_msg:
            escaped = html_escape(last_msg)
            max_body = 4000 - len(header) - 20  # margin for tags
            if len(escaped) > max_body:
                escaped = escaped[:max_body - 3] + "..."
            header += f"\n<i>{escaped}</i>"

        return header, None

    return f"\U0001F514 <b>{event}</b>", None


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    try:
        event = hook_input.get("hook_event_name", "")
        _logger.info(f"Event: {event}, keys: {list(hook_input.keys())}")
        if event == "Stop":
            _logger.info(f"Stop data: stop_hook_active={hook_input.get('stop_hook_active')}, "
                         f"last_assistant_message length={len(hook_input.get('last_assistant_message', ''))}, "
                         f"last_assistant_message preview={repr(hook_input.get('last_assistant_message', '')[:200])}")

        config = load_config()
        session_name = get_session_name()
        topic_id = get_topic_id(session_name)
        _logger.info(f"Session: {session_name}, topic_id: {topic_id}")

        # Clear busy reaction on events that mean Claude paused/stopped
        if event in ("Stop", "PermissionRequest", "Notification"):
            clear_busy(session_name, config)

        text, reply_markup = format_notification(hook_input, session_name)
        _logger.info(f"Formatted text length: {len(text) if text else 0}")
        if text:
            send_telegram(config, text, reply_markup, topic_id)
            _logger.info("Sent to Telegram")
    except Exception as e:
        _logger.error(f"Error: {e}", exc_info=True)
        pass  # Fire-and-forget: never fail the hook

    # Output empty JSON
    print("{}")


if __name__ == "__main__":
    main()
