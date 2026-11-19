"""Writes /workspace/scene_report.json whenever the scene is saved, and once at startup.

The eval grades what the saved scene holds, not what the agent says it did. Blender has no accessibility tree, so
the report is the only trustworthy read-out of it: object transforms, the camera's pose at fixed frames, and the
scene's frame range and resolution. Coordinates are rounded to four decimals so two equal scenes report equal.
"""

import json
import os
from pathlib import Path

import bpy
from mathutils import Vector

# Frames the report samples. Fixed, so every save of every trial reports the same rows.
FRAMES = (1, 12, 24, 36, 48, 60, 72, 84, 96)
WORKSPACE = Path(os.environ.get("PARALLAX_WORKSPACE", "/workspace"))


def _rounded(values) -> list[float]:
    return [round(float(value), 4) for value in values]


def snapshot() -> None:
    scene = bpy.context.scene
    camera = scene.camera
    samples = []
    if camera is not None:
        current = scene.frame_current
        for frame in FRAMES:
            scene.frame_set(frame)
            # Constraints and parented transforms only reach `matrix_world` after a depsgraph update, which a
            # headless or background session does not run on its own; without this every sample repeats frame 1.
            bpy.context.view_layer.update()
            matrix = camera.matrix_world
            forward = (matrix.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()
            samples.append(
                {"frame": frame, "location": _rounded(matrix.translation), "forward": _rounded(forward)}
            )
        scene.frame_set(current)
    report = {
        "scene": {
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        },
        "camera": camera.name if camera is not None else None,
        "samples": samples,
        "objects": [
            {"name": obj.name, "location": _rounded(obj.location), "dimensions": _rounded(obj.dimensions)}
            for obj in bpy.data.objects
        ],
    }
    (WORKSPACE / "scene_report.json").write_text(json.dumps(report, indent=1) + "\n")


def _on_save(*_args) -> None:
    snapshot()


bpy.app.handlers.save_post.append(_on_save)
snapshot()
