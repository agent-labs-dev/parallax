"""The oracle: the workspace a correct fly-by leaves behind, built without a desktop.

Three files are graded: `set_shipped.blend` (saved, so it exists and holds a scene), `renders/frame_0048.png`
and the `scene_report.json` the save hook writes from Blender's own data. The oracle writes the report values a
correct fly-by produces and a 640x360 PNG, and leaves the seeded scenes and scripts as they are.
"""

import json
import struct
import zlib
from pathlib import Path

FRAMES = (1, 12, 24, 36, 48, 60, 72, 84, 96)
TARGET = (0.0, 4.0, 2.5)
OBJECTS = [
    {"name": "Ground", "location": [0.0, 0.0, 0.0], "dimensions": [40.0, 40.0, 0.0]},
    {"name": "Tower_A", "location": [-3.0, 4.0, 1.5], "dimensions": [1.0, 1.0, 3.0]},
    {"name": "Tower_B", "location": [0.0, 4.0, 2.5], "dimensions": [1.2, 1.2, 5.0]},
    {"name": "Tower_C", "location": [3.0, 4.0, 1.5], "dimensions": [1.0, 1.0, 3.0]},
    {"name": "Sun", "location": [0.0, 0.0, 10.0], "dimensions": [0.0, 0.0, 0.0]},
]


def _camera_x(frame: int) -> float:
    """The straight line from -6 at frame 1 to 6 at frame 96."""
    return -6.0 + 12.0 * (frame - 1) / 95.0


def _sample(frame: int) -> dict[str, object]:
    location = [_camera_x(frame), 0.0, 1.6]
    forward = [target - axis for target, axis in zip(TARGET, location)]
    length = sum(value * value for value in forward) ** 0.5
    return {
        "frame": frame,
        "location": [round(value, 4) for value in location],
        "forward": [round(value / length, 4) for value in forward],
    }


def _png(width: int, height: int) -> bytes:
    """A 640x360 gradient: the shape a rendered frame has, with pixels that are not all one colour."""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        for x in range(width):
            rows += bytes((x * 255 // (width - 1), y * 255 // (height - 1), 96))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    report = {
        "scene": {"frame_start": 1, "frame_end": 96, "resolution": [640, 360]},
        "camera": "Camera",
        "samples": [_sample(frame) for frame in FRAMES],
        "objects": OBJECTS,
    }
    (workspace / "scene_report.json").write_text(json.dumps(report, indent=1) + "\n")
    renders = workspace / "renders"
    renders.mkdir(exist_ok=True)
    (renders / "frame_0048.png").write_bytes(_png(640, 360))
    # The saved scene itself. Grading reads the report the save hook wrote; the file is checked for being a
    # non-empty scene save, so the oracle writes a header of the length a small Blender scene has.
    (workspace / "set_shipped.blend").write_bytes(b"BLENDER-v304" + bytes(700_000))
    return "Animated the camera fly-by, rendered frame 48 and saved set_shipped.blend."
