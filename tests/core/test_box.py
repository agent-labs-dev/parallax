"""Boxes: process control, snapshots and trusted verify.

Docker tests never pull or build (the suite runs offline): they use an image already present
locally, named by PARALLAX_TEST_SH_IMAGE (an image with sh, cp and util-linux setsid supporting -w).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from parallax import __version__, egress
from parallax import box as boxes
from parallax.box import (
    PIPE_QUIET_S,
    BoxError,
    DockerBox,
    HostBox,
    Sidecar,
    base_image_tag,
    box_image_tag,
    desktop_image_tag,
    device_box,
    docker,
    snapshot,
    verify_docker,
    verify_host,
)
from parallax.harnesses.base import config_hash
from parallax.harnesses.manifest import harness_from_manifest
from parallax.models import (
    Capability,
    CommandCheck,
    CostSource,
    FailureMode,
    HarnessInfo,
    HarnessKind,
    Isolation,
    RemoteBox,
)

SH_IMAGE = os.environ.get("PARALLAX_TEST_SH_IMAGE", f"parallax-runtime:{__version__}")


@pytest.fixture
def box(tmp_path: Path) -> HostBox:
    return HostBox(tmp_path / "box", Isolation.HOST)


def command_check(*run: str, timeout_s: int = 30) -> CommandCheck:
    return CommandCheck(id="verify", kind="command", mode=next(iter(FailureMode)), run=run, timeout_s=timeout_s)


async def test_host_spawn_passes_stdin_env_and_cwd(box: HostBox) -> None:
    script = "import os,sys; print(sys.stdin.read().upper(), os.environ['TRIAL'], os.getcwd(), os.environ['HOME'])"
    proc = await box.spawn([sys.executable, "-c", script], env={"TRIAL": "t1"}, cwd=box.workspace)
    code, out, _ = await proc.communicate(b"hello", timeout_s=30)
    assert code == 0
    assert out.split() == ["HELLO", "t1", str(box.workspace), str(box.home)]


async def test_host_env_does_not_leak_provider_keys(box: HostBox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-host-secret")
    proc = await box.spawn(
        [sys.executable, "-c", "import os; print('OPENAI_API_KEY' in os.environ)"], env={}, cwd=box.workspace
    )
    assert (await proc.communicate(None, 30))[1].strip() == "False"


async def test_host_timeout_kills_the_whole_process_tree(box: HostBox, tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.pid"
    # The grandchild would outlive a plain kill of its parent; the process-group kill must take it.
    script = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open({str(marker)!r},'w').write(str(p.pid)); "
        "print('started', flush=True); time.sleep(60)"
    )
    proc = await box.spawn([sys.executable, "-c", script], env={}, cwd=box.workspace)
    started = time.monotonic()
    code, out, _ = await proc.communicate(None, timeout_s=1.5)
    assert code is None and "started" in out
    assert time.monotonic() - started < 10
    grandchild = int(marker.read_text())
    time.sleep(0.2)
    assert not pid_alive(grandchild)


async def test_exited_process_keeps_a_descendants_late_output_by_default(box: HostBox) -> None:
    # A wrapper that exits before its worker prints: a grading check's output must not be cut off
    # by the quiet window a harness CLI opts into.
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.6); print(\\'late\\', flush=True)']); "
        "print('early', flush=True)"
    )
    proc = await box.spawn([sys.executable, "-c", script], env={}, cwd=box.workspace)
    code, out, _ = await proc.communicate(None, timeout_s=30)
    assert (code, out.split()) == (0, ["early", "late"])


async def test_exited_process_does_not_wait_out_a_descendant_holding_its_pipes(box: HostBox, tmp_path: Path) -> None:
    marker = tmp_path / "lingerer.pid"
    # The CLI exits, but a child it left behind keeps stdout and stderr open: the runner must not
    # charge the turn for however long that child lives.
    script = (
        "import subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); open({str(marker)!r},'w').write(str(p.pid)); "
        "print('answer', flush=True)"
    )
    proc = await box.spawn([sys.executable, "-c", script], env={}, cwd=box.workspace)
    try:
        code, out, _ = await proc.communicate(None, timeout_s=30, quiet_s=PIPE_QUIET_S)
        assert proc.exited_at is not None
        assert (code, out.strip()) == (0, "answer")
        assert time.monotonic() - proc.exited_at < 1.0
    finally:
        os.kill(int(marker.read_text()), signal.SIGKILL)


def pid_alive(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


async def test_write_file_is_confined_to_workspace_and_home(box: HostBox) -> None:
    await box.write_file(box.workspace / "attachments" / "a.csv", b"x,y\n")
    assert (box.workspace / "attachments" / "a.csv").read_bytes() == b"x,y\n"
    with pytest.raises(ValueError, match="outside"):
        await box.write_file(box.workspace / ".." / ".." / "escape.txt", b"no")


async def test_snapshot_round_trip_skips_escaping_symlinks(box: HostBox, tmp_path: Path) -> None:
    (box.workspace / "src").mkdir()
    (box.workspace / "src" / "app.py").write_text("print('hi')\n")
    (box.workspace / "data.bin").write_bytes(bytes(range(256)))
    (box.workspace / "link").symlink_to("/etc/passwd")
    out = await snapshot(box, tmp_path / "trial")
    assert (tmp_path / "trial" / "workspace.tar.gz").is_file()
    assert (out / "src" / "app.py").read_text() == "print('hi')\n"
    assert (out / "data.bin").read_bytes() == bytes(range(256))
    assert not (out / "link").exists()


async def test_host_verify_runs_on_a_copy(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "f.txt").write_text("42")
    script = (
        "import pathlib; p=pathlib.Path('f.txt'); print(p.read_text()); p.write_text('mutated'); raise SystemExit(3)"
    )
    outcome = await verify_host(snap, command_check(sys.executable, "-c", script))
    assert (outcome.exit, outcome.stdout.strip(), outcome.error) == (3, "42", None)
    assert (snap / "f.txt").read_text() == "42"
    missing = await verify_host(snap, command_check("definitely-not-a-binary-px"))
    assert missing.error is not None and missing.exit is None
    slow = await verify_host(snap, command_check(sys.executable, "-c", "import time; time.sleep(30)", timeout_s=1))
    assert slow.exit is None and slow.error is None and "timeout" in slow.stderr


def local_image(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


@pytest.mark.docker
async def test_docker_verify_is_offline_and_read_only(tmp_path: Path) -> None:
    if not local_image(SH_IMAGE):
        pytest.skip(f"{SH_IMAGE} not present locally (set PARALLAX_TEST_SH_IMAGE)")
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "f.txt").write_text("42\n")
    # Only loopback exists under --network none; the write lands in the tmpfs copy.
    check = command_check("sh", "-c", "cat f.txt; echo x > f.txt; ls /sys/class/net; exit 4")
    outcome = await verify_docker(SH_IMAGE, snap, check)
    assert outcome.exit == 4 and outcome.error is None
    assert outcome.stdout.split() == ["42", "lo"]
    assert (snap / "f.txt").read_text() == "42\n"
    missing = await verify_docker("parallax-no-such-image:0", snap, check)
    assert missing.error is not None


async def test_docker_box_rejects_env_the_docker_client_reads(tmp_path: Path) -> None:
    # Values travel through the docker client's own environment, so these would retarget the client.
    for name in ("PATH", "HOME", "DOCKER_HOST"):
        with pytest.raises(BoxError, match=name):
            await DockerBox("px-never-started").spawn(["true"], env={name: "x"}, cwd=tmp_path)


def host_cmdlines() -> bytes:
    out = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        with contextlib.suppress(OSError):  # processes exit mid-scan
            out.append(path.read_bytes())
    return b"".join(out)


@pytest.mark.docker
async def test_docker_box_spawn_kill_and_snapshot(tmp_path: Path) -> None:
    if not local_image(SH_IMAGE):
        pytest.skip(f"{SH_IMAGE} not present locally (set PARALLAX_TEST_SH_IMAGE)")
    name = f"px-test-box-{os.getpid()}"
    await docker(
        "run", "-d", "--name", name, "--network", "none", SH_IMAGE, "sh", "-c", "mkdir -p /workspace && sleep 600"
    )
    box = DockerBox(name, user="root")
    try:
        await box.write_file(Path("/workspace/notes.txt"), b"hello\n")
        secret = f"sk-{secrets.token_hex(8)}"
        proc = await box.spawn(
            ["sh", "-c", 'cat notes.txt; echo "$TRIAL"; sleep 61'], env={"TRIAL": secret}, cwd=Path("/workspace")
        )
        assert secret.encode() not in host_cmdlines()  # env values never reach the host process list
        code, out, _ = await proc.communicate(None, timeout_s=2)
        assert code is None and out.split() == ["hello", secret]
        # Slim images lack ps; /proc is universal.
        cmdlines = await docker("exec", name, "sh", "-c", "cat /proc/[0-9]*/cmdline | tr '\\0' ' '")
        assert "sleep 61" not in cmdlines
        out_dir = await snapshot(box, tmp_path / "trial")
        assert (out_dir / "notes.txt").read_text() == "hello\n"
    finally:
        await box.close()


EGRESS_PROBE = """
import urllib.error, urllib.request
for url in ("https://example.com", "http://1.1.1.1", "http://sidecar:7801/mcp", "http://sidecar:7802/_health"):
    try:
        print(url, urllib.request.urlopen(url, timeout=5).status)
    except urllib.error.HTTPError as exc:
        print(url, exc.code)
    except OSError:
        print(url, "unreachable")
"""


@pytest.mark.docker
async def test_box_network_reaches_only_the_sidecar() -> None:
    # This is what makes docker isolation's web_isolation=enforced a fact rather than a claim.
    image = base_image_tag()
    if not local_image(image):
        pytest.skip(f"{image} not built for this source tree (any `--isolation docker` run builds it)")
    sidecar = Sidecar(secrets.token_urlsafe(16), secrets.token_urlsafe(16))
    await sidecar.up(f"test-{secrets.token_hex(3)}")
    try:
        net = sidecar.network
        assert net is not None
        name = f"{net}-box-probe"
        await docker("run", "-d", "--name", name, "--label", f"parallax.net={net}", "--network", net, image,
                     "sleep", "infinity")  # fmt: skip
        out = await docker("exec", name, "python", "-c", EGRESS_PROBE, timeout_s=60)
    finally:
        await sidecar.down()
    status = dict(line.rsplit(" ", 1) for line in out.splitlines())
    assert status["https://example.com"] == "unreachable" and status["http://1.1.1.1"] == "unreachable"
    assert status["http://sidecar:7801/mcp"] == "401"  # the world, refusing a missing bearer token
    assert status["http://sidecar:7802/_health"] == "200"


def test_sidecar_allowlist_composes_harness_hosts_with_model_hosts() -> None:
    sidecar = Sidecar(
        secrets.token_urlsafe(16), secrets.token_urlsafe(16), egress_hosts=("api.product.example", "registry.npmjs.org")
    )

    assert sidecar.allowed_hosts == ("api.product.example", "openrouter.ai", "registry.npmjs.org")
    assert egress.allowed("api.product.example", 443, sidecar.allowed_hosts)
    assert egress.allowed("registry.npmjs.org", 443, sidecar.allowed_hosts)
    assert not egress.allowed("example.com", 443, sidecar.allowed_hosts)


async def test_sidecar_trial_network_is_private_and_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args: str, **kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(boxes, "docker", fake_docker)
    sidecar = Sidecar("world", "meter")
    sidecar.container = "run-sidecar"
    network = await sidecar.trial_network("trial-1")
    assert network.startswith("px-trial-")
    assert calls[0][:3] == ("network", "create", "--internal")
    assert calls[1] == ("network", "connect", "--alias", "sidecar", network, "run-sidecar")
    await sidecar.remove_trial_network(network)
    assert calls[-2:] == [
        ("network", "disconnect", network, "run-sidecar"),  # docker takes NETWORK CONTAINER
        ("network", "rm", network),
    ]


async def test_orphan_trial_network_sweep_keeps_live_sidecar_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args[:3] == ("network", "ls", "--filter"):
            return "orphan\nlive\n"
        if args[:2] == ("network", "inspect"):
            owner = "100" if args[-1] == "orphan" else "101"
            return json.dumps({"Labels": {"parallax.owner-pid": owner}, "Containers": {}})
        return ""

    monkeypatch.setattr(boxes, "docker", fake_docker)

    def fake_kill(pid: int, _signal: int) -> None:
        if pid == 100:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(boxes.os, "kill", fake_kill)
    await boxes.sweep_orphan_trial_networks()
    assert ("network", "rm", "orphan") in calls
    assert ("network", "rm", "live") not in calls


def remote_info(remote_box: RemoteBox) -> HarnessInfo:
    return HarnessInfo(
        name="remote", kind=next(iter(HarnessKind)), capabilities=frozenset(), pins_model=True,
        cost_source=CostSource.HARNESS, isolation=Isolation.REMOTE, remote_box=remote_box,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("remote_box", "isolation", "expected"),
    [
        (RemoteBox.DEVICE, Isolation.REMOTE, True),
        (RemoteBox.DEVICE, Isolation.DOCKER, False),
        (RemoteBox.HOST, Isolation.REMOTE, False),
    ],
)
def test_every_remote_trial_of_a_device_harness_gets_a_device_box(
    remote_box: RemoteBox, isolation: Isolation, expected: bool
) -> None:
    # Its agent has a shell whatever the trial requires; without a box it would run on a shared device.
    assert device_box(remote_info(remote_box), isolation) is expected


def test_remote_box_left_at_its_default_does_not_change_a_harness_identity() -> None:
    default = remote_info(RemoteBox.HOST)
    assert "remote_box" not in default.model_dump()
    assert config_hash(default) != config_hash(remote_info(RemoteBox.DEVICE))


def test_desktop_layer_does_not_change_the_default_image_tag() -> None:
    info = remote_info(RemoteBox.DEVICE)
    desktop = info.model_copy(update={"capabilities": info.capabilities | {Capability.DESKTOP}})
    assert box_image_tag(info) == box_image_tag(desktop)
    assert desktop_image_tag(desktop) != box_image_tag(desktop)


def test_a_tasks_own_desktop_packages_get_an_image_of_their_own() -> None:
    info = remote_info(RemoteBox.DEVICE)
    shared = desktop_image_tag(info)
    blender = desktop_image_tag(info, ["blender", "libgl1-mesa-dri"])
    assert blender == desktop_image_tag(info, ["libgl1-mesa-dri", "blender"])  # order is not identity
    assert blender != shared  # the shared layer's tag is untouched
    assert blender != desktop_image_tag(info, ["blender"])  # one list, one image
    assert desktop_image_tag(info, []) == shared


def test_a_hyprland_desktop_is_its_own_image() -> None:
    info = remote_info(RemoteBox.DEVICE)
    tag = desktop_image_tag(info, session="hyprland")
    assert tag.endswith("-hyprland")
    assert tag not in {desktop_image_tag(info), box_image_tag(info)}
    assert desktop_image_tag(info, ["libreoffice-fresh"], "hyprland") != tag
    assert desktop_image_tag(info, ["b", "a"], "hyprland") == desktop_image_tag(info, ["a", "b"], "hyprland")


def test_a_hyprland_box_gets_the_vkms_node_and_its_group_and_nothing_else(tmp_path: Path) -> None:
    drm, dev = tmp_path / "drm", tmp_path / "dev"
    dev.mkdir()
    for name, driver in (("card0", "i915"), ("card1", "vkms"), ("card1-Virtual-1", "vkms")):
        (drm / name).mkdir(parents=True)
        target = tmp_path / "drivers" / driver
        target.mkdir(parents=True, exist_ok=True)
        (drm / name / "device").symlink_to(target)
    (dev / "card1").write_text("")
    assert boxes.vkms_device(drm, dev) == dev / "card1"
    (drm / "card1" / "device").unlink()
    (drm / "card1" / "device").symlink_to(tmp_path / "drivers" / "i915")
    with pytest.raises(BoxError, match="modprobe vkms"):
        boxes.vkms_device(drm, dev)


def test_only_a_hyprland_task_asks_docker_for_a_device() -> None:
    from parallax.task import load_task

    assert boxes.desktop_run_args(load_task(Path("tasks/core/files-desktop-fill-expense-form"))) == ()


@pytest.mark.docker
async def test_docker_box_processes_run_on_the_world_clock() -> None:
    image = f"parallax-runtime:{__version__}"
    if not local_image(image):
        pytest.skip(f"{image} not built (any `--isolation docker` run builds it)")
    name = f"px-test-clock-{os.getpid()}"
    await docker("run", "-d", "--name", name, "--network", "none", image, "sleep", "600")
    box = DockerBox(name, user="agent", clock=True)
    try:
        if "px-clock" not in await docker("exec", name, "ls", "/usr/local/bin"):
            pytest.skip(f"{image} predates px-clock; rebuild it")
        world_now = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)
        # A child process (the shell's python) must agree with its parent: the clock is per tree.
        script = "python3 -c 'import time; print(time.time())'; node -e 'console.log(Date.now() / 1000)'"
        for target in (world_now, world_now + timedelta(days=1)):
            await box.set_clock(target)
            proc = await box.spawn(["sh", "-c", script], env={}, cwd=Path("/"))
            code, out, err = await proc.communicate(None, timeout_s=30)
            assert code == 0, err
            for seen in map(float, out.split()):
                assert abs(seen - target.timestamp()) < 30
    finally:
        await box.close()


async def test_device_sweep_keeps_live_owners_and_removes_exited_or_reused_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_docker(*args: str) -> str:
        calls.append(args)
        return "alive 10 original\ndead 11 original\nreused 12 original" if args[0] == "ps" else ""

    async def stamp(pid: int) -> str | None:
        return {10: "original", 11: None, 12: "new-owner"}[pid]

    monkeypatch.setattr(boxes, "docker", fake_docker)
    monkeypatch.setattr(boxes, "process_start_stamp", stamp)
    await boxes.sweep_orphan_device_boxes()
    assert calls[1:] == [("rm", "-f", "dead"), ("rm", "-f", "reused")]
    assert f"label={boxes.DEVICE_OWNER_HOST}={socket.gethostname()}" in calls[0]


@pytest.mark.parametrize("fails", [False, True])
async def test_device_box_seeds_before_desktop_start_and_removes_failed_boxes(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    from parallax.task import load_task

    task = load_task(Path("tasks/core/files-desktop-fill-expense-form"))
    events = []

    async def boot(task, image, box, run_args, command):
        events.append("seed")
        return box

    async def start(box, task):
        assert box.desktop
        events.append("desktop")
        if fails:
            raise BoxError("desktop app gate: crashed")

    async def close(box):
        events.append("close")

    async def no_orphan_sweep() -> None:
        """`create_device` reaps orphaned device boxes first; that fleet-wide `docker ps -a`
        is not what this test is about, so it is stubbed out. The suite promises to run
        offline and without a reachable Docker daemon, and an unstubbed sweep made this
        test fail on any host whose docker socket is not readable."""

    monkeypatch.setattr(DockerBox, "boot", staticmethod(boot))
    monkeypatch.setattr(DockerBox, "start_desktop", start)
    monkeypatch.setattr(DockerBox, "close", close)
    monkeypatch.setattr(boxes, "sweep_orphan_device_boxes", no_orphan_sweep)
    if fails:
        with pytest.raises(BoxError, match="desktop app gate"):
            await DockerBox.create_device(task, "image")
        assert events == ["seed", "desktop", "close"]
    else:
        box = await DockerBox.create_device(task, "image")
        assert events == ["seed", "desktop"]
        await box.close()


def test_a_foreign_platform_build_never_shares_the_native_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    from parallax import box

    monkeypatch.setattr(box.platform, "machine", lambda: "arm64")
    assert box._foreign(None) == ""
    assert box._foreign(box.DESKTOP_PLATFORM) == "-amd64"
    monkeypatch.setattr(box.platform, "machine", lambda: "x86_64")
    assert box._foreign(box.DESKTOP_PLATFORM) == ""


@pytest.mark.parametrize(
    ("desktop", "fails", "kept"), [(True, False, True), (True, True, False), (False, False, False)]
)
async def test_a_desktop_box_keeps_its_display_recording_and_a_lost_one_never_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, desktop: bool, fails: bool, kept: bool
) -> None:
    calls: list[str] = []

    async def fake_docker(*args: str, timeout_s: float = 600.0) -> str:
        calls.append(args[0])
        if fails:
            raise BoxError("no such file")
        if args[0] == "cp":
            Path(args[-1]).write_bytes(b"mp4")
        return ""

    monkeypatch.setattr(boxes, "docker", fake_docker)
    dest = tmp_path / "desktop.mp4"
    got = await DockerBox("box", desktop=desktop).keep_recording(dest)
    assert (got == dest) is kept
    # ffmpeg is stopped before the copy, so the file it finishes is the one kept.
    assert calls == (["exec"] if fails else ["exec", "cp"] if desktop else [])


def stop_recording(tmp_path: Path, *, pid: int | None, written: bool) -> subprocess.CompletedProcess[str]:
    video = tmp_path / "recording.mp4"
    if pid is not None:
        (tmp_path / "recording.mp4.pid").write_text(str(pid))
    if written:
        video.write_bytes(b"mp4")
    return subprocess.run(["sh", "-c", boxes.STOP_RECORDING, "sh", str(video)], capture_output=True, text=True)


def test_a_recording_that_cannot_be_stopped_says_why_on_stderr(tmp_path: Path) -> None:
    missing = stop_recording(tmp_path, pid=None, written=False)
    assert missing.returncode == 1
    assert "recorder never started" in missing.stderr
    recorder = subprocess.Popen(["sleep", "30"])
    threading.Thread(target=recorder.wait, daemon=True).start()  # reaped on exit, so the stop script sees it end
    empty = stop_recording(tmp_path, pid=recorder.pid, written=False)
    assert empty.returncode == 1
    assert "recorder exited without writing" in empty.stderr
    recorder = subprocess.Popen(["sleep", "30"])
    threading.Thread(target=recorder.wait, daemon=True).start()
    assert stop_recording(tmp_path, pid=recorder.pid, written=True).returncode == 0


async def test_a_lost_recording_logs_the_reason_docker_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_docker(*args: str, timeout_s: float = 600.0) -> str:
        raise BoxError("docker exec -u failed (1): recorder never started")

    monkeypatch.setattr(boxes, "docker", fake_docker)
    with caplog.at_level("WARNING", logger="parallax.box"):
        assert await DockerBox("box", desktop=True).keep_recording(tmp_path / "desktop.mp4") is None
    assert "recorder never started" in caplog.text


def test_every_box_image_runs_the_harness_install_line_as_the_manifest_wrote_it() -> None:
    # The manifest's install line owns its npm script policy; an image-wide override would run every dependency's.
    run = 'RUN if [ -n "$HARNESS_INSTALL" ]; then sh -c "$HARNESS_INSTALL"; fi'
    for name in ("box.Dockerfile", "desktop-hyprland.Dockerfile"):
        assert run in (Path(boxes.__file__).parent / name).read_text(), name


def test_the_claude_code_install_allows_only_its_own_package_scripts() -> None:
    # npm 12 blocks install scripts by default; claude-code's postinstall places its native binary.
    install = harness_from_manifest("claude-code").info.install
    assert install == "npm i -g @anthropic-ai/claude-code@2.1.325 --allow-scripts=@anthropic-ai/claude-code"


async def test_hyprland_boxes_take_the_one_display_in_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rm(*_args: object, **_kwargs: object) -> str:
        return ""

    monkeypatch.setattr("parallax.box.docker", rm)
    first, second = DockerBox("a", session="hyprland"), DockerBox("b", session="hyprland")
    await first.take_seat()
    waiting = asyncio.create_task(second.take_seat())
    await asyncio.sleep(0.05)
    assert not waiting.done()
    await first.close()
    await asyncio.wait_for(waiting, 1)
    await second.close()


async def test_a_failed_docker_command_reports_what_the_failing_step_printed(tmp_path, monkeypatch) -> None:
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\necho 'npm error 404 nebula-ai not found'\necho 'returned a non-zero code: 1' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    with pytest.raises(boxes.BoxError) as caught:
        await boxes.docker("build", "-f", "box.Dockerfile", ".")
    assert "returned a non-zero code: 1" in str(caught.value)
    assert "npm error 404 nebula-ai not found" in str(caught.value)
