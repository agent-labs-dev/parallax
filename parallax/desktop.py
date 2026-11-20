"""The device box's private desktop session; no host display or session bus is used.

x11 (the default): `serve` runs under dbus-run-session, starting Xvfb, openbox and the accessibility bus.
hyprland: `serve` starts deskpilot's standalone session (its own Hyprland on the host's vkms display, bus and
accessibility bus) and publishes the Wayland variables that reach it.
`exec` joins the session before launching a box command, so the device daemon and its cua-driver or deskpilot child
inherit the same bus and display. State and logs live outside the graded workspace.
"""

from __future__ import annotations

import contextlib
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
RECORDING = str(STATE / "recording.mp4")  # the display's video, kept as the trial's desktop.mp4
KIND = STATE / "kind"  # the session kind `serve` started, read by `exec` and `record`
SESSION_START_TIMEOUT = 120
WINDOW_TIMEOUT = 60


def session_env(kind: str = "x11") -> dict[str, str]:
    """What a process in the session starts with. x11 sets its display and drops the host's Wayland one; hyprland has
    no display of its own until `serve` publishes the session's Wayland variables, which `exec` adds."""
    if kind == "hyprland":
        env = {**os.environ, "SHELL": "/bin/sh"}
        env.pop("DISPLAY", None)
        return env
    env = {**os.environ, **SESSION_ENV}
    env.pop("WAYLAND_DISPLAY", None)
    return env


def saved_kind() -> str:
    return KIND.read_text() if KIND.exists() else "x11"


def deskpilot(*argv: str, env: dict[str, str], timeout: float, cwd: str | None = None) -> str:
    """One deskpilot command in the session; a failure carries deskpilot's own message."""
    done = subprocess.run(
        ["deskpilot", "--session", *argv], env=env, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )
    if done.returncode:
        raise RuntimeError(
            f"desktop session gate: deskpilot {' '.join(argv)}: {(done.stdout + done.stderr).strip()[-600:]}"
        )
    return done.stdout


def serve_hyprland(spec: Desktop) -> None:
    ready = STATE / "ready"
    env = session_env("hyprland")
    try:
        reach: dict[str, str] = json.loads(deskpilot("--json", "env", env=env, timeout=SESSION_START_TIMEOUT))
        env |= reach
        if spec.launch_by == "box":
            for app in spec.apps:
                deskpilot("launch", "/usr/bin/python3", app.path, env=env, timeout=WINDOW_TIMEOUT, cwd="/workspace")
            deadline = time.monotonic() + WINDOW_TIMEOUT
            while True:
                titles = {w["title"] for w in json.loads(deskpilot("--json", "windows", env=env, timeout=30))}
                if all(app.title in titles for app in spec.apps):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("desktop app gate: seeded windows did not open")
                time.sleep(0.2)
        # Publish last: neither the daemon nor other commands can join a half-started session.
        (STATE / "env.json").write_text(json.dumps(reach))
        ready.write_text(str(os.getpid()))
        while True:
            if subprocess.run(["hyprctl", "version"], env=env, capture_output=True, timeout=10).returncode:
                raise RuntimeError("desktop session gate: Hyprland exited (see /tmp/px-desktop.log)")
            time.sleep(1)
    except Exception as exc:
        (STATE / "error").write_text(str(exc))
        raise
    finally:
        ready.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            deskpilot("stop", env=env, timeout=30)


def serve(spec: Desktop) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    KIND.write_text(spec.session)
    if spec.session == "hyprland":
        return serve_hyprland(spec)
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


def recorder_argv(kind: str, dest: str, env: dict[str, str]) -> list[str]:
    """The command that records the session's display to `dest` until interrupted: five frames a second on one thread
    keeps it light beside the app. It is a record of the run, never evidence a check reads."""
    if kind == "hyprland":
        monitors = json.loads(subprocess.check_output(["hyprctl", "-j", "monitors"], env=env, timeout=10))
        if not monitors:
            sys.exit("desktop recording: the session has no output")
        return ["wf-recorder", "-o", monitors[0]["name"], "-r", "5", "-f", dest]
    return [
        *("ffmpeg", "-nostdin", "-loglevel", "error", "-f", "x11grab", "-framerate", "5", "-video_size", "1280x800"),
        *("-i", ":99", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p", "-threads", "1"),
        dest,
    ]


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
            env = session_env(saved_kind()) | json.loads((STATE / "env.json").read_text())
            os.execvpe(argv[0], argv, env)
        case ["record", dest]:
            env = session_env(saved_kind()) | json.loads((STATE / "env.json").read_text())
            argv = recorder_argv(saved_kind(), dest, env)
            os.execvpe(argv[0], argv, env)
        case _:
            sys.exit("usage: python -m parallax.desktop serve SPEC | status | exec COMMAND ... | record FILE")


if __name__ == "__main__":
    main()
