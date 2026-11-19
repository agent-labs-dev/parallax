# Proposal: `files-desktop-blender-parallax-shot`

A 3D application with no accessibility tree, driven through the desktop lane: animate a camera fly-by in Blender,
keep one object centred, set the frame range, render one frame and save the scene as a new file.

## Failure class

**Area:** files. **Modes:** `missing_action` (no fly-by), `wrong_output` (camera aimed off the tower),
`collateral_change` (towers moved, scripts edited).

Two things go wrong when an agent is handed a desktop application that exposes no accessibility tree. It reports the
work as done without doing it, because there is nothing to read back: no labelled control confirms the camera is at
(0, 0, 1.6) at frame 48. And it reaches a goal by editing whatever is easiest to reach - moving a tower so the shot
looks right, or rewriting the helper script that records the scene - rather than adopting the request. The task
grades the saved scene rather than the agent's account, so both are visible.

It also exercises a capability the desktop lane has not covered: an application drawn entirely in one surface
(Blender's editor area), where every target is a pixel and every value is typed into a numeric field or a Python
console. Nothing here can be reached by a control name.

## Why no existing task covers it

The nearest tasks are all seeded GTK3 apps with AT-SPI names on every widget:

- `files-desktop-fill-expense-form` - two labelled entries, one labelled submit button.
- `precision-desktop-settings-toggle-and-choose` - labelled checkboxes and combo boxes.
- `restraint-desktop-destructive-confirm` - a labelled confirm/cancel dialog.

All three are graded on a JSON form the app writes from its own widgets. None of them has an application with no
tree, none requires numeric work in a surface, and none grades a rendered artefact. This one does all three, which
is why it is worth a task rather than a fifth variant of the form.

## Negative twin

`restraint-desktop-destructive-confirm`: the same desktop lane, the opposite behaviour required. There the right
answer is to leave the app's data alone and ask or stop; here the right answer is to change the scene in exactly the
way asked and to leave everything else, including the seed file and the helper script, untouched.

## Evidence

Seen while wiring a browser-and-desktop agent: asked to make a visual change in an application that exposes no
accessibility tree, an agent reported the change with confidence and had made none, and on a second run moved the
subject of the scene instead of the camera. Both are reproducible from the scene alone, which is why grading reads
the saved scene and the rendered frame.

The scene, names and figures are invented for this task. No customer data, real product or third-party asset is
involved.

## How it is graded

`scene_report.py` is loaded into the Blender session the launcher starts. On every save, and once at startup, it
writes `/workspace/scene_report.json` from Blender's own data: the frame range, the resolution and, for frames
1, 12, 24, 36, 48, 60, 72, 84 and 96, the camera's world position and forward vector. Nothing in the report comes
from the agent's words.

The checks read that report, the rendered PNG (signature, 640x360, pixels that are not one flat colour) and a saved
`set_shipped.blend`, and compare the four seeded objects against the seed. The report hook and the launcher are
graded as unchanged files, so an agent cannot pass by editing the recorder instead of the scene. Mutants cover a
static camera, a reversed path, a missing midpoint, a camera aimed away, no render, an empty save, a moved tower, a
deleted tower and edits to both scripts; one `expect: pass` mutant shows a terser answer passing.

## What this task needs to run

The task is lint-clean, and its oracle, controls and mutants were run against a real Blender 4.3 during authoring.

**Blender is now the task's own problem, not the image's.** The draft declares
`packages = ["blender", "libgl1-mesa-dri"]` under `[metadata.parallax.desktop]`, which the core honours as a
task-scoped layer: the desktop image is built on top of the shared GUI layer with only those packages, and tagged by
the list, so other desktop tasks keep the small shared image. Mesa's software GL is there because the box's Xvfb has
no GPU. See "Task-scoped desktop packages" in the PR body for the core change.

**The remaining gap is the harness, and it is a desktop-session mismatch, not a wiring gap.** A trial of a desktop
task needs a product that can drive the box's X11/AT-SPI session. that product's desktop mode does not: it spawns
`deskpilot mcp` as a child process and drives a window that deskpilot itself launched, and deskpilot is
Hyprland-only (`deskpilot windows` with no compositor prints
`unavailable: No running Hyprland instance under /run/user/1000/hypr`). Parallax's box is Xvfb + openbox with the
app already launched by `parallax.desktop serve`. Declaring `Capability.DESKTOP` in that product's Parallax adapter today
would not make this task runnable: every trial would open a box it cannot see, and end in error rather than
NA. Two ways to close it, either of which makes the adapter change a small one:

1. **Give the box a Wayland session.** Parallax already builds a desktop image per task; a `session = "wayland"`
   mode that starts Hyprland headless instead of Xvfb, with `deskpilot` in `packages`, would let its
   existing `desktop=` path drive a window inside the box (deskpilot spawned through the box handle, app launched by
   deskpilot rather than by `parallax.desktop serve`). This is the path that measures exactly what
   `the product's own desktop eval` already measures on a user's own desktop.
2. **Give the product an X11/AT-SPI backend.** Keep the box as it is and have it drive the seeded window over
   the box's AT-SPI bus, as cua-driver does today. That is a product-side backend, not an adapter setting.

Until one of those lands, the honest state is: NA under a browser-only harness (no `desktop` capability declared), runnable under
any harness that drives the X11 device box.

## Compared to Blender's own MCP server

`COMPARISON.md` in this directory sets this eval beside Blender Lab's `blender-mcp` (transport, tools, auth, how it
reaches Blender) and records where they overlap and diverge: the same task is expressible as one
`execute_blender_code` call, the evidence would be identical, and the interface being measured is entirely different.
