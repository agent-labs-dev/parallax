"""The device box's private X11 session; no host display or session bus is used.

`serve` runs under dbus-run-session. `exec` joins that session before launching a
box command, so the device daemon and its cua-driver child inherit the same bus.
State and logs live outside the graded workspace.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from parallax.models import Desktop

STATE = Path("/home/agent/.parallax-desktop")
SESSION_ENV = {
    "DISPLAY": ":99",
    "GDK_BACKEND": "x11",
    "GTK_MODULES": "gail:atk-bridge",
    "NO_AT_BRIDGE": "0",
    "SHELL": "/bin/sh",
    "CUA_DRIVER_PERMISSION_MODE": "standard",
}
EXEC = ("python", "-m", "parallax.desktop", "exec")


def session_env() -> dict[str, str]:
    env: dict[str, str] = {**os.environ, **SESSION_ENV}
    env.pop("WAYLAND_DISPLAY", None)
    return env


def serve(spec: Desktop) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    env = session_env()
    children: list[subprocess.Popen[bytes]] = []
    ready = STATE / "ready"
    try:
        children.append(subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"], env=env))
        deadline = time.monotonic() + 30
        while subprocess.run(["xdpyinfo"], env=env, capture_output=True, timeout=5).returncode:
            if children[0].poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("desktop display gate: Xvfb did not become ready")
            time.sleep(0.1)
        for argv in (
            ["openbox", "--sm-disable"],
            ["/usr/libexec/at-spi-bus-launcher", "--launch-immediately"],
            ["/usr/libexec/at-spi2-registryd", "--use-gnome-session=false"],
        ):
            children.append(subprocess.Popen(argv, env=env))
        apps = [subprocess.Popen(["/usr/bin/python3", app.path], cwd="/workspace", env=env) for app in spec.apps]
        children.extend(apps)
        while True:
            windows = subprocess.check_output(["xwininfo", "-root", "-tree"], env=env, timeout=5).decode()
            if all(f'"{app.title}"' in windows for app in spec.apps):
                break
            if any(p.poll() is not None for p in apps) or time.monotonic() >= deadline:
                raise RuntimeError("desktop app gate: seeded windows did not open")
            time.sleep(0.1)
        # Publish last: neither the daemon nor other commands can join a half-started session.
        (STATE / "env.json").write_text(
            json.dumps({**SESSION_ENV, "DBUS_SESSION_BUS_ADDRESS": env["DBUS_SESSION_BUS_ADDRESS"]})
        )
        ready.write_text(str(os.getpid()))
        while True:
            if any(p.poll() is not None for p in children):
                raise RuntimeError("desktop session gate: a session process exited (see /tmp/px-desktop.log)")
            time.sleep(0.2)
    except Exception as exc:
        (STATE / "error").write_text(str(exc))
        raise
    finally:
        ready.unlink(missing_ok=True)
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
        for proc in children:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def status() -> str:
    if (STATE / "error").exists():
        return (STATE / "error").read_text()
    if (STATE / "ready").exists():
        try:
            os.kill(int((STATE / "ready").read_text()), 0)
        except ProcessLookupError:
            return "desktop session gate: session exited"
        return "ready"
    return "starting"


def main() -> None:
    match sys.argv[1:]:
        case ["serve", raw]:
            serve(Desktop.model_validate_json(raw))
        case ["status"]:
            print(status())
        case ["exec", *argv] if argv:
            if status() != "ready":
                sys.exit(f"desktop session gate: {status()}")
            env = session_env() | json.loads((STATE / "env.json").read_text())
            os.execvpe(argv[0], argv, env)
        case _:
            sys.exit("usage: python -m parallax.desktop serve SPEC | status | exec COMMAND ...")


if __name__ == "__main__":
    main()
