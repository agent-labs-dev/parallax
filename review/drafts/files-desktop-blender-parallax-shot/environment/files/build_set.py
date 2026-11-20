"""Builds the seeded scene, `set.blend`, in whatever Blender the box installed.

The scene is built from this script rather than shipped as a file so the seed opens in the same Blender that
writes it, whatever version that is: a `.blend` saved by one release is not reliably readable by another.
"""

import os

import bpy
from mathutils import Vector

# The box builds the seed in /workspace; the task's own scene builder builds it in a scratch directory.
TARGET = os.path.join(os.environ.get("PARALLAX_SET_DIR", "/workspace"), "set.blend")


def _cube(name: str, location: tuple[float, float, float], dimensions: tuple[float, float, float]) -> None:
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.dimensions = dimensions


for existing in list(bpy.data.objects):
    bpy.data.objects.remove(existing, do_unlink=True)

bpy.ops.mesh.primitive_plane_add(size=40.0, location=(0.0, 0.0, 0.0))
bpy.context.active_object.name = "Ground"
_cube("Tower_A", (-3.0, 4.0, 1.5), (1.0, 1.0, 3.0))
_cube("Tower_B", (0.0, 4.0, 2.5), (1.2, 1.2, 5.0))
_cube("Tower_C", (3.0, 4.0, 1.5), (1.0, 1.0, 3.0))

bpy.ops.object.light_add(type="SUN", location=(0.0, 0.0, 10.0))
bpy.context.active_object.name = "Sun"

bpy.ops.object.camera_add(location=(-6.0, 0.0, 1.6))
camera = bpy.context.active_object
camera.name = "Camera"
camera.rotation_mode = "XYZ"
camera.rotation_euler = (Vector((0.0, 4.0, 2.5)) - camera.location).to_track_quat("-Z", "Y").to_euler()
bpy.context.scene.camera = camera

scene = bpy.context.scene
scene.frame_start = 1
scene.frame_end = 1
scene.frame_set(1)
scene.render.resolution_x = 640
scene.render.resolution_y = 360
scene.render.resolution_percentage = 100
# Workbench draws a frame in a second on the box's software renderer, where EEVEE takes minutes.
scene.render.engine = "BLENDER_WORKBENCH"
bpy.ops.wm.save_as_mainfile(filepath=TARGET, compress=False)
