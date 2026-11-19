"""Builds the oracle's saved scene and render, and the mutants' wrong scenes, in a real Blender.

The checks open `set_shipped.blend` in Blender, so the oracle and every scene mutant must be real saved scenes of
the Blender the box installs. Run it in the task's verify image (`environment/Dockerfile`), from the task directory:

    docker run --rm -v "$PWD:/task" -w /task <verify image> \
        blender --background --factory-startup --python solution/build_scenes.py

It builds the seed with `environment/files/build_set.py`, then writes `solution/set_shipped.blend`,
`solution/frame_0048.png` and one `tests/mutants/<id>.blend` per scene mutant (plus a flat render).
"""

import os
import runpy
import struct
import tempfile
import zlib
from pathlib import Path

import bpy

TASK = Path.cwd()
SOLUTION = TASK / "solution"
MUTANTS = TASK / "tests" / "mutants"


def seed() -> None:
    """A fresh copy of the seeded scene, built by the same script the box's launcher runs."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["PARALLAX_SET_DIR"] = tmp
        runpy.run_path(str(TASK / "environment" / "files" / "build_set.py"))
        bpy.ops.wm.open_mainfile(filepath=str(Path(tmp) / "set.blend"))


def key_path(points: list[tuple[int, tuple[float, float, float]]]) -> None:
    camera = bpy.data.objects["Camera"]
    for frame, location in points:
        camera.location = location
        camera.keyframe_insert("location", frame=frame)
    for curve in camera.animation_data.action.fcurves:
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def track(target: str) -> None:
    constraint = bpy.data.objects["Camera"].constraints.new("TRACK_TO")
    constraint.target = bpy.data.objects[target]
    constraint.track_axis = "TRACK_NEGATIVE_Z"
    constraint.up_axis = "UP_Y"


def flyby() -> None:
    key_path([(1, (-6.0, 0.0, 1.6)), (96, (6.0, 0.0, 1.6))])
    track("Tower_B")


def finish(path: Path, render: Path | None = None) -> None:
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 96
    if render is not None:
        scene.frame_set(48)
        scene.render.filepath = str(render)
        bpy.ops.render.render(write_still=True)
    scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True, copy=True)


def flat_png(path: Path, width: int = 640, height: int = 360) -> None:
    """A valid 640x360 PNG of one colour: the size of a render with nothing rendered in it."""
    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")
    )


def static() -> None:
    bpy.context.scene.frame_end = 1


def reversed_path() -> None:
    key_path([(1, (6.0, 0.0, 1.6)), (96, (-6.0, 0.0, 1.6))])
    track("Tower_B")


def bowed_path() -> None:
    key_path([(1, (-6.0, 0.0, 1.6)), (48, (0.0, -2.0, 2.6)), (96, (6.0, 0.0, 1.6))])
    track("Tower_B")


def looks_away() -> None:
    key_path([(1, (-6.0, 0.0, 1.6)), (96, (6.0, 0.0, 1.6))])
    bpy.data.objects["Camera"].rotation_euler = (1.5708, 0.0, 3.1416)


def moves_a_tower() -> None:
    bpy.data.objects["Tower_B"].location.x = 0.5
    flyby()


def deletes_a_tower() -> None:
    bpy.data.objects.remove(bpy.data.objects["Tower_C"], do_unlink=True)
    flyby()


def parents_a_tower() -> None:
    """Tower_A keeps its own location but rides a moved parent, so only its world position changes."""
    bpy.ops.object.empty_add(location=(0.0, 2.0, 0.0))
    holder = bpy.context.active_object
    holder.name = "Holder"
    tower = bpy.data.objects["Tower_A"]
    tower.parent = holder
    flyby()


SOLUTION.mkdir(exist_ok=True)
MUTANTS.mkdir(exist_ok=True)
seed()
flyby()
finish(SOLUTION / "set_shipped.blend", SOLUTION / "frame_0048.png")
for name, build in (
    ("never-animates", static),
    ("reversed-path", reversed_path),
    ("bowed-path", bowed_path),
    ("looks-away", looks_away),
    ("moves-a-tower", moves_a_tower),
    ("deletes-a-tower", deletes_a_tower),
    ("parents-a-tower", parents_a_tower),
):
    seed()
    build()
    finish(MUTANTS / f"{name}.blend")
flat_png(MUTANTS / "flat-render.png")
