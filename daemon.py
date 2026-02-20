#!/usr/bin/env python3
"""Telegram polling daemon for Claude Code bridge.

Polls Telegram for incoming messages from forum topics, maps topic_id
to session, and injects text into the correct Zellij pane.

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
import subprocess
import urllib.request
import urllib.error
import fcntl
import logging
from logging.handlers import RotatingFileHandler

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")
SESSIONS_FILE = os.path.join(BRIDGE_DIR, "sessions.json")


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


def zellij_write_bytes(env, *byte_args):
    """Write raw bytes to a Zellij session."""
    subprocess.run(
        ["zellij", "action", "write"] + [str(b) for b in byte_args],
        env=env, timeout=5, capture_output=True,
    )


def inject_into_zellij(zellij_session, text):
    """Inject text into a Zellij session via write-chars + Enter."""
    env = os.environ.copy()
    env["ZELLIJ_SESSION_NAME"] = zellij_session

    try:
        subprocess.run(
            ["zellij", "action", "write-chars", text],
            env=env, timeout=5, capture_output=True,
        )
        zellij_write_bytes(env, 13)
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error(f"Zellij injection failed for {zellij_session}: {e}")
        return False


def inject_selection_into_zellij(zellij_session, index, num_defined_options=4):
    """Select an option in AskUserQuestion UI.

    Claude Code's Select context accepts:
    - Number keys (1-9) to jump to defined options
    - Arrow Down/Up (or J/K) to navigate
    - Enter to confirm

    Defined options (index < num_defined_options) use number keys.
    Built-in options (Other, Chat) use arrow navigation.
    """
    env = os.environ.copy()
    env["ZELLIJ_SESSION_NAME"] = zellij_session

    try:
        if index < num_defined_options:
            # Defined options: use number key (1-indexed)
            number = str(index + 1)
            subprocess.run(
                ["zellij", "action", "write-chars", number],
                env=env, timeout=5, capture_output=True,
            )
        else:
            # Built-in options (Other, Chat): navigate with arrow keys
            # Down arrow = ESC [ B = bytes 27 91 66
            for _ in range(index):
                zellij_write_bytes(env, 27, 91, 66)
                time.sleep(0.05)
            # Press Enter
            zellij_write_bytes(env, 13)
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error(f"Zellij selection failed for {zellij_session}: {e}")
        return False


def inject_permission_into_zellij(zellij_session, choice):
    """Handle permission prompt selection using number keys.

    Permission prompt order is always: 1=Yes, 2=Always Allow, 3=No
    Uses same number key approach as AskUserQuestion (proven to work).
    """
    env = os.environ.copy()
    env["ZELLIJ_SESSION_NAME"] = zellij_session

    perm_map = {"yes": "1", "always": "2", "no": "3"}
    number = perm_map.get(choice)
    if not number:
        return False

    try:
        subprocess.run(
            ["zellij", "action", "write-chars", number],
            env=env, timeout=5, capture_output=True,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error(f"Zellij permission failed for {zellij_session}: {e}")
        return False


def handle_sessions_command(topic_id=None):
    """Handle /sessions command."""
    sessions = load_sessions()
    active = {n: i for n, i in sessions.items() if i.get("active", True)}
    if not active:
        send_to_topic(topic_id, "No active sessions.")
        return

    lines = ["<b>Active Sessions:</b>"]
    for name, info in active.items():
        cwd = info.get("cwd", "?")
        started = info.get("started_at", "?")
        has_topic = "\u2705" if info.get("topic_id") else "\u274c"
        lines.append(f"\n\u2022 <b>{name}</b> {has_topic}\n  <i>{cwd}</i>\n  Started: {started}")
    send_to_topic(topic_id, "\n".join(lines))


def handle_help_command(topic_id=None):
    """Handle /help command."""
    send_to_topic(topic_id,
        "<b>Telegram-Claude Bridge</b>\n\n"
        "<b>Forum Topics Mode:</b>\n"
        "Each session has its own topic. Just type your reply in the topic \u2014 no prefix needed.\n\n"
        "<b>Commands:</b>\n"
        "/sessions - List active sessions\n"
        "/help - Show this help"
    )


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

    # Handle commands
    if text.startswith("/sessions"):
        handle_sessions_command(topic_id)
        return
    if text.startswith("/help") or text.startswith("/start"):
        handle_help_command(topic_id)
        return

    # Find session by topic
    session_name, session_info = find_session_by_topic(topic_id)

    if not session_name:
        send_to_topic(topic_id, "\u26a0\ufe0f No session linked to this topic.")
        return

    if not session_info.get("active", True):
        send_to_topic(topic_id, f"\u26a0\ufe0f Session <b>{session_name}</b> is not active.")
        return

    zellij_session = session_info.get("zellij_session", "")
    if not zellij_session:
        send_to_topic(topic_id, f"\u26a0\ufe0f Session <b>{session_name}</b> has no Zellij session.")
        return

    # Inject into Zellij with [Telegram] prefix
    prefixed_text = f"[Telegram] {text}"
    if inject_into_zellij(zellij_session, prefixed_text):
        send_to_topic(topic_id, f"\u2705 <code>{text[:200]}</code>")
        logger.info(f"Injected into {zellij_session}: {prefixed_text}")
    else:
        send_to_topic(topic_id, f"\u274c Failed to send. Is the Zellij session alive?")


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
    zellij_session = session_info.get("zellij_session", "")
    topic_id = session_info.get("topic_id")

    if not zellij_session:
        telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "No Zellij session"})
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

    if action_type == "perm":
        # Permission prompt: use Y/N keys or arrow navigation
        if inject_permission_into_zellij(zellij_session, action_value):
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Selected: {button_text[:50]}"})
            send_to_topic(topic_id, f"\u2705 Selected: <code>{button_text[:100]}</code>")
            logger.info(f"Permission '{action_value}' injected into {zellij_session}")
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

        if inject_selection_into_zellij(zellij_session, index, num_defined):
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Selected: {button_text[:50]}"})
            send_to_topic(topic_id, f"\u2705 Selected: <code>{button_text[:100]}</code>")
            logger.info(f"Selection {index} injected into {zellij_session}: {button_text}")
        else:
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Failed to send"})
            send_to_topic(topic_id, f"\u274c Failed to send.")
    else:
        if inject_into_zellij(zellij_session, action_value):
            telegram_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Sent: {action_value[:50]}"})
            send_to_topic(topic_id, f"\u2705 <code>{action_value[:100]}</code>")
            logger.info(f"Button tap injected into {zellij_session}: {action_value}")
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
