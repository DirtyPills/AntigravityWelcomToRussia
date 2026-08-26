#!/usr/bin/env python3
"""Run the configured desktop application as the systemd unit's unprivileged user."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import sys
from pathlib import Path


CONFIG = Path("/etc/agy-net/desktop.json")
ALLOWED_ENVIRONMENT = {
    "HOME", "USER", "LOGNAME", "SHELL", "PATH", "DISPLAY", "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "LANG", "LC_ALL",
}


def fail(message: str) -> None:
    print(f"agy-net desktop launcher: {message}", file=sys.stderr)
    raise SystemExit(2)


def load_config(expected_user: str) -> tuple[str, dict[str, str]]:
    try:
        info = CONFIG.lstat()
    except FileNotFoundError:
        fail(f"missing {CONFIG}; run sudo agy-net desktop-configure")
    if CONFIG.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        fail(f"unsafe configuration file: {CONFIG}")
    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"could not read configuration: {exc}")
    if not isinstance(value, dict) or set(value) != {"binary", "environment", "user"}:
        fail("invalid configuration shape")
    binary, user, environment = value["binary"], value["user"], value["environment"]
    if not isinstance(binary, str) or not binary.startswith("/") or "\x00" in binary:
        fail("invalid executable path")
    if not isinstance(user, str) or user != expected_user:
        fail("the requested systemd user does not match the configured desktop user")
    if not isinstance(environment, dict) or not set(environment).issubset(ALLOWED_ENVIRONMENT):
        fail("invalid desktop environment")
    checked_environment: dict[str, str] = {}
    for key, item in environment.items():
        if not isinstance(item, str) or "\x00" in item or "\n" in item or "\r" in item:
            fail(f"invalid value for {key}")
        checked_environment[key] = item
    if checked_environment.get("USER") != user or checked_environment.get("LOGNAME") != user:
        fail("desktop environment user does not match configuration")
    try:
        binary_info = Path(binary).stat()
    except FileNotFoundError:
        fail(f"configured executable no longer exists: {binary}")
    if not stat.S_ISREG(binary_info.st_mode) or not os.access(binary, os.X_OK):
        fail(f"configured executable is not runnable: {binary}")
    return binary, checked_environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--validate", action="store_true", help="validate configuration without executing Antigravity")
    args = parser.parse_args()
    try:
        actual_user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        fail("systemd service UID has no passwd entry")
    if args.user != actual_user or actual_user == "root":
        fail("refusing a mismatched or root service user")
    binary, environment = load_config(actual_user)
    if args.validate:
        return 0
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    os.execve(binary, [binary], environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
