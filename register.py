#!/usr/bin/env python3
"""Session register/deregister for Claude Code SessionStart/SessionEnd hooks.

Reads hook JSON from stdin, manages sessions.json with forum topic mapping,
sends Telegram notifications, and auto-starts the polling daemon if not running.

Only tmux sessions are supported. Non-tmux sessions silently update existing
entries (matched by session_id or cwd) but never create new topics.
"""

import json
import os
import sys
import fcntl
import subprocess
import urllib.request
import urllib.error
import time

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")
SESSIONS_FILE = os.path.join(BRIDGE_DIR, "sessions.json")
DAEMON_SCRIPT = os.path.join(BRIDGE_DIR, "daemon.py")


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def telegram_api(config, method, params):
    """Call Telegram Bot API. Returns parsed response or None on error."""
    try:
        url = f"https://api.telegram.org/bot{config['bot_token']}/{method}"
        data = json.dumps(params).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def send_to_topic(config, topic_id, text):
    """Send a message to a specific forum topic."""
    params = {
        "chat_id": config["group_chat_id"],
        "text": text,
        "parse_mode": "HTML",
    }
    # General topic (id=1) doesn't accept message_thread_id
    if topic_id and topic_id != 1:
        params["message_thread_id"] = topic_id
    telegram_api(config, "sendMessage", params)


def create_forum_topic(config, name):
    """Create a forum topic in the group. Returns topic_id or None."""
    result = telegram_api(config, "createForumTopic", {
        "chat_id": config["group_chat_id"],
        "name": name,
    })
    if result and result.get("ok"):
        return result["result"]["message_thread_id"]
    return None


def reopen_forum_topic(config, topic_id):
    """Reopen a closed forum topic."""
    telegram_api(config, "reopenForumTopic", {
        "chat_id": config["group_chat_id"],
        "message_thread_id": topic_id,
    })


def close_forum_topic(config, topic_id):
    """Close a forum topic."""
    telegram_api(config, "closeForumTopic", {
        "chat_id": config["group_chat_id"],
        "message_thread_id": topic_id,
    })


def get_tmux_session_name():
    """Get tmux session name. Returns None if not inside tmux."""
    if not os.environ.get("TMUX"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def load_sessions():
    """Load sessions.json with file locking."""
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


def find_existing_entry(sessions, session_id, cwd):
    """Find an existing session entry by session_id or cwd.

    Returns (name, info) or (None, None).
    """
    # Match by session_id first
    for name, info in sessions.items():
        if info.get("session_id") == session_id:
            return name, info
    # Match by active cwd
    for name, info in sessions.items():
        if info.get("cwd") == cwd and info.get("active"):
            return name, info
    return None, None


def daemon_is_running(config):
    """Check if daemon is running by PID file."""
    pid_file = config.get("pid_file", os.path.join(BRIDGE_DIR, "daemon.pid"))
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def start_daemon():
    """Auto-start the daemon if not running."""
    subprocess.Popen(
        [sys.executable, DAEMON_SCRIPT, "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    config = load_config()
    event = hook_input.get("hook_event_name", "")
    session_id = hook_input.get("session_id", "unknown")
    cwd = hook_input.get("cwd", "")

    tmux_name = get_tmux_session_name()
    sessions = load_sessions()

    if event == "SessionStart":
        if tmux_name:
            # Running inside tmux — full registration
            session_name = tmux_name
            topic_display = f"tmux_{session_name}"

            topic_id = None
            if session_name in sessions and sessions[session_name].get("topic_id"):
                topic_id = sessions[session_name]["topic_id"]
                reopen_forum_topic(config, topic_id)
            else:
                topic_id = create_forum_topic(config, topic_display)

            sessions[session_name] = {
                "session_id": session_id,
                "tmux_session": tmux_name,
                "backend": "tmux",
                "cwd": cwd,
                "topic_id": topic_id,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "active": True,
            }
            save_sessions(sessions)

            if topic_id:
                send_to_topic(config, topic_id, f"\u2705 Session started\n<i>{cwd}</i>")
        else:
            # Not in tmux — only update existing entry, never create new one
            name, info = find_existing_entry(sessions, session_id, cwd)
            if name and info:
                info["session_id"] = session_id
                info["cwd"] = cwd
                info["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                info["active"] = True
                sessions[name] = info
                save_sessions(sessions)

                topic_id = info.get("topic_id")
                if topic_id:
                    reopen_forum_topic(config, topic_id)
                    send_to_topic(config, topic_id, f"\u2705 Session started\n<i>{cwd}</i>")

        # Auto-start daemon
        if not daemon_is_running(config):
            start_daemon()

    elif event == "SessionEnd":
        # Find the session — by tmux name or by session_id/cwd lookup
        session_name = tmux_name
        if not session_name:
            name, _ = find_existing_entry(sessions, session_id, cwd)
            session_name = name

        if session_name and session_name in sessions:
            topic_id = sessions[session_name].get("topic_id")
            sessions[session_name]["active"] = False
            save_sessions(sessions)

            if topic_id:
                send_to_topic(config, topic_id, "\u274c Session ended")
                close_forum_topic(config, topic_id)

    # Output empty JSON
    print("{}")


if __name__ == "__main__":
    main()
