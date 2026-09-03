"""Test suite, run inside Blender:

    blender -b --factory-startup -P tests/run_tests.py -- [reader|build|all]

The fixture recording is generated here rather than committed: it exercises
whatever Rerun SDK is actually installed instead of a frozen file, and it keeps
real recordings out of the repository (see ``tools/check_no_data.py``).
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

FPS = 30.0
FAILURES = []
PASSES = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} — {detail}")


def close(a, b, tol=1e-4):
    return abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


def write_fixture(path):
    """A small recording covering every archetype the add-on claims to import."""
    import numpy as np
    import rerun as rr

    rec = rr.RecordingStream("rrd_importer_test")
    rec.save(path)

    rec.log("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)

    colors = [[255, 0, 0], [0, 255, 0], [0, 0, 255], [128, 64, 32]]
    for step in range(3):
        rec.set_time("t", duration=float(step))
        base = float(step)
        rec.log(
            "/world/map",
            rr.Points3D(
                positions=[[base, 0, 0], [base, 1, 0], [base, 0, 1], [base, 1, 1]],
                colors=colors,
                radii=[0.1, 0.1, 0.1, 0.1],
            ),
        )
        rec.log(
            "/world/drone",
            rr.Transform3D(
                translation=[base, base * 2.0, 1.0],
                rotation=rr.Quaternion(xyzw=[0.0, 0.0, 0.0, 1.0]),
            ),
        )
        rec.log(
            "/world/path",
            rr.LineStrips3D(
                [[[0, 0, 0]] + [[i + 1.0, 0, 0] for i in range(step + 1)]],
                colors=[[0, 200, 255]],
                radii=[0.05],
            ),
        )
        # One colour for three points: Rerun clamps the last value across the
        # rest, and so must the reader.
        rec.log(
            "/world/clamped",
            rr.Points3D(
                positions=[[10, 0, base], [10, 1, base], [10, 2, base]],
                colors=[[10, 20, 30]],
            ),
        )
        rec.log("/plots/height", rr.Scalars(base * 3.0))

    rec.set_time("t", duration=1.0)
    rec.log(
        "/world/boxes",
        rr.Boxes3D(
            centers=[[5, 0, 0], [5, 3, 0]],
            half_sizes=[[0.5, 0.5, 1.0], [1.0, 1.0, 1.0]],
            colors=[[255, 128, 0], [0, 128, 255]],
        ),
    )
    rec.log(
        "/world/mesh",
        rr.Mesh3D(
            vertex_positions=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
            triangle_indices=[[0, 1, 2]],
            vertex_colors=[[255, 0, 255], [255, 0, 255], [255, 0, 255]],
        ),
    )
    rec.log(
        "/world/arrows",
        rr.Arrows3D(
            origins=[[0, 0, 0], [0, 0, 1]],
            vectors=[[2, 0, 0], [0, 3, 0]],
            colors=[[255, 255, 0], [0, 255, 255]],
        ),
    )
    rec.log("/world/drone/cam", rr.Pinhole(focal_length=400.0, width=800, height=600))

    rec.flush()
    del rec
    return path


# ---------------------------------------------------------------------------
# reader
# ---------------------------------------------------------------------------


def test_broadcast():
    """Unit-test the clamping rule directly.

    Rerun's "clamped" component semantics let one colour stand for a whole
    row of points, but the SDK often materialises them at log time — so a
    fixture cannot be relied on to exercise this path.
    """
    import numpy as np

    from rerun_importer.rrd_reader import _broadcast_per_point

    print("clamping:")
    one_for_three = _broadcast_per_point(
        np.array([[7, 8, 9]], np.uint8), np.array([1]), np.array([3]), 3, np.uint8
    )
    check("one value clamps across the row",
          one_for_three.tolist() == [[7, 8, 9]] * 3, one_for_three.tolist())

    partial = _broadcast_per_point(
        np.array([[1, 1, 1], [2, 2, 2]], np.uint8), np.array([2]), np.array([4]), 3, np.uint8
    )
    check("the last value fills the remainder",
          partial.tolist() == [[1, 1, 1], [2, 2, 2], [2, 2, 2], [2, 2, 2]],
          partial.tolist())

    surplus = _broadcast_per_point(
        np.arange(9, dtype=np.uint8).reshape(3, 3), np.array([3]), np.array([2]), 3, np.uint8
    )
    check("surplus values are dropped, not misaligned",
          surplus.tolist() == [[0, 1, 2], [3, 4, 5]], surplus.tolist())

    multi = _broadcast_per_point(
        np.array([[5, 5, 5], [6, 6, 6]], np.uint8),
        np.array([1, 1]), np.array([2, 3]), 3, np.uint8,
    )
    check("rows stay independent",
          multi.tolist() == [[5, 5, 5]] * 2 + [[6, 6, 6]] * 3, multi.tolist())

    empty = _broadcast_per_point(
        np.array([[9, 9, 9]], np.uint8), np.array([0, 1]), np.array([0, 2]), 3, np.uint8
    )
    check("an empty row consumes nothing",
          empty.tolist() == [[9, 9, 9]] * 2, empty.tolist())


def test_reader(path):
    import numpy as np

    from rerun_importer import rrd_reader

    print("reader:")
    probe = rrd_reader.probe(path)
    check("probe finds the t timeline", any(t.name == "t" for t in probe.timelines),
          [t.name for t in probe.timelines])
    check("default timeline is not log_tick",
          rrd_reader.default_timeline(probe.timelines).name != "log_tick")

    scene = rrd_reader.read(path, timeline="t")
    check("view coordinates decoded", scene.view_coordinates == "RFU", scene.view_coordinates)
    check("no unsupported archetypes leaked", not scene.skipped, scene.skipped)

    cloud = next((c for c in scene.clouds if c.path == "/world/map"), None)
    check("point cloud found", cloud is not None)
    if cloud:
        check("every row's points are kept", cloud.count == 12, cloud.count)
        check("point positions survive", close(cloud.positions[4][0], 1.0),
              cloud.positions[:5].tolist())
        check("colours decode as RGBA", list(cloud.colors[0]) == [255, 0, 0, 255],
              cloud.colors[0].tolist())
        check("mid-tone colour read back", list(cloud.colors[3]) == [128, 64, 32, 255],
              cloud.colors[3].tolist())
        check("radii read back", close(cloud.radii[0], 0.1), cloud.radii[:3].tolist())
        births = sorted(set(int(b) for b in cloud.birth))
        check("birth times are the row times", births == [0, 1_000_000_000, 2_000_000_000],
              births)

    clamped = next((c for c in scene.clouds if c.path == "/world/clamped"), None)
    check("clamped colours fan out over every point", clamped is not None
          and len(clamped.colors) == clamped.count
          and all(list(c) == [10, 20, 30, 255] for c in clamped.colors),
          None if clamped is None else clamped.colors[:4].tolist())

    xform = next((t for t in scene.transforms if t.path == "/world/drone"), None)
    check("transform series found", xform is not None)
    if xform:
        check("one sample per row", len(xform.times) == 3, len(xform.times))
        check("translation read back", close(xform.translations[2][1], 4.0),
              xform.translations.tolist())
        check("rotation read back", xform.quaternions is not None or xform.matrices is not None)
        check("series is recognised as animated", not xform.static)

    lines = next((l for l in scene.lines if l.path == "/world/path"), None)
    check("line strips found", lines is not None)
    if lines:
        check("final strip is the longest", len(lines.strips[0]) == 4,
              [len(s) for s in lines.strips])
        check("growing strip keeps per-vertex birth",
              int(lines.birth[0][0]) == 0 and int(lines.birth[0][3]) == 2_000_000_000,
              lines.birth[0].tolist())
        check("line colour read back", list(lines.colors[0]) == [0, 200, 255, 255],
              lines.colors[0].tolist())

    arrows = next((l for l in scene.lines if l.path == "/world/arrows"), None)
    check("arrows found", arrows is not None,
          [l.path for l in scene.lines])
    if arrows:
        check("one two-point strip per arrow",
              len(arrows.strips) == 2 and all(len(s) == 2 for s in arrows.strips),
              [len(s) for s in arrows.strips])
        check("arrow tip is origin + vector",
              close(arrows.strips[1][0][2], 1.0) and close(arrows.strips[1][1][1], 3.0),
              arrows.strips[1].tolist())
        check("arrow colours read back", list(arrows.colors[1]) == [0, 255, 255, 255],
              arrows.colors[1].tolist())

    boxes = next((b for b in scene.boxes if b.path == "/world/boxes"), None)
    check("boxes found", boxes is not None)
    if boxes:
        check("both boxes read", len(boxes.centers) == 2, len(boxes.centers))
        check("half sizes read back", close(boxes.half_sizes[0][2], 1.0),
              boxes.half_sizes.tolist())
        check("box colours read back", list(boxes.colors[1]) == [0, 128, 255, 255],
              boxes.colors[1].tolist())

    mesh = next((m for m in scene.meshes if m.path == "/world/mesh"), None)
    check("mesh found", mesh is not None)
    if mesh:
        check("mesh vertices read", len(mesh.vertices) == 3, len(mesh.vertices))
        check("mesh triangles read", mesh.triangles is not None and len(mesh.triangles) == 1,
              None if mesh.triangles is None else mesh.triangles.tolist())

    pinhole = next((p for p in scene.pinholes if p.path == "/world/drone/cam"), None)
    check("pinhole found", pinhole is not None)
    if pinhole:
        check("focal length read", close(pinhole.focal[0], 400.0), pinhole.focal)
        check("resolution read", close(pinhole.resolution[0], 800.0), pinhole.resolution)

    scalars = next((s for s in scene.scalars if s.path == "/plots/height"), None)
    check("scalars found", scalars is not None)
    if scalars:
        check("scalar values read", close(scalars.values[-1], 6.0), scalars.values.tolist())

    # A point budget must actually bite, and must keep the data consistent.
    capped = rrd_reader.read(path, timeline="t", max_points=5)
    capped_cloud = next(c for c in capped.clouds if c.path == "/world/map")
    check("point budget caps the cloud", capped_cloud.count == 5, capped_cloud.count)
    check("point budget keeps arrays aligned",
          len(capped_cloud.birth) == 5 and len(capped_cloud.colors) == 5)

    filtered = rrd_reader.read(path, timeline="t", include=["/world/map"])
    check("entity filter excludes the rest",
          [c.path for c in filtered.clouds] == ["/world/map"] and not filtered.boxes,
          [e.path for e in filtered.all_entities()])


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build(path):
    import bpy

    from rerun_importer import importer, nodegroups

    print("build:")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    options = importer.Options(timeline="t", fps=FPS, trail_frames=0.0, import_scalars=True)
    report = importer.import_rrd(path, options)

    names = {obj.name: obj for obj in bpy.data.objects}
    check("point cloud object built", "map" in names, sorted(names))
    check("line strips object built", "path" in names)
    check("boxes object built", "boxes" in names)
    check("arrows object built", "arrows" in names)
    check("mesh object built", "mesh" in names)
    check("pinhole camera built", "cam" in names)
    check("framing camera added", "Rerun Camera" in names)

    drone = names.get("drone")
    check("transform entity built", drone is not None)
    if drone:
        check("pinhole is parented under its transform",
              names["cam"].parent is drone, getattr(names["cam"].parent, "name", None))
        action = drone.animation_data.action if drone.animation_data else None
        check("transform is keyframed", action is not None)
        if action:
            from rerun_importer.scene_builder import _action_fcurves

            curves = _action_fcurves(action)
            check("keyframes on location", any(c.data_path == "location" for c in curves),
                  [c.data_path for c in curves])
            location = next(c for c in curves if c.data_path == "location")
            check("one keyframe per row", len(location.keyframe_points) == 3,
                  len(location.keyframe_points))
            check("keyframes land on the right frames",
                  close(location.keyframe_points[1].co[0], 1 + FPS),
                  [kp.co[0] for kp in location.keyframe_points])
            check("data is interpolated linearly, not eased",
                  all(kp.interpolation == "LINEAR" for kp in location.keyframe_points))

    cloud = names.get("map")
    if cloud:
        mesh = cloud.data
        check("all points baked into one mesh", len(mesh.vertices) == 12, len(mesh.vertices))
        birth = mesh.attributes.get(nodegroups.BIRTH_ATTR)
        check("birth attribute written", birth is not None)
        if birth:
            values = [birth.data[i].value for i in range(len(mesh.vertices))]
            check("birth converted to Blender frames",
                  close(min(values), 1.0) and close(max(values), 1 + 2 * FPS),
                  (min(values), max(values)))
        colour = mesh.attributes.get(nodegroups.COLOR_ATTR)
        check("colour attribute written", colour is not None)
        if colour:
            red = colour.data[0].color
            check("saturated colour survives intact",
                  close(red[0], 1.0, 1e-3) and red[1] < 0.01, list(red))
            # (128, 64, 32) sRGB is (0.2158, 0.0513, 0.0144) linear; skipping
            # the conversion would leave (0.502, 0.251, 0.125).
            midtone = colour.data[3].color
            check("colour converted sRGB -> linear",
                  close(midtone[0], 0.21586, 2e-3) and close(midtone[1], 0.05126, 2e-3),
                  list(midtone))
        mods = [m for m in cloud.modifiers if m.type == "NODES"]
        check("geometry nodes modifier attached", len(mods) == 1)
        if mods:
            from rerun_importer.scene_builder import _read_modifier_input

            material = _read_modifier_input(mods[0], mods[0].node_group, "Material")
            # A Material socket that silently fails to set leaves the material
            # with no users, so Blender purges it on save and every render comes
            # out black.  This is that regression, pinned down.
            check("material reaches the modifier socket", material is not None, material)
            trail = _read_modifier_input(mods[0], mods[0].node_group, "Trail")
            check("trail input set", trail is not None and close(trail, 0.0), trail)

    check("a material exists and is used",
          any(m.users > 0 for m in bpy.data.materials),
          [(m.name, m.users) for m in bpy.data.materials])

    check("scene frame range covers the recording",
          bpy.context.scene.frame_end == int(1 + 2 * FPS), bpy.context.scene.frame_end)
    check("scene fps set", close(bpy.context.scene.render.fps, FPS))
    check("report counts the points", report.points == 21, report.points)
    check("report counts keyframes", report.keyframes == 3, report.keyframes)

    # Time filtering must actually remove geometry, or "animated" is a lie.
    # The line object is checked through the depsgraph (its nodes output a
    # mesh, which Python can count); the point cloud is checked through the
    # render, since a geometry-nodes point cloud is not reachable from
    # object.data.
    line_counts = []
    for frame in (1, int(1 + 2 * FPS)):
        bpy.context.scene.frame_set(frame)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = names["path"].evaluated_get(depsgraph)
        line_counts.append(len(evaluated.data.vertices))
    check("the trajectory grows over time", 0 < line_counts[0] < line_counts[1],
          line_counts)

    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = 160, 90
    scene.render.image_settings.file_format = "PNG"

    def render_at(frame):
        scene.frame_set(frame)
        scene.render.filepath = os.path.join(
            tempfile.gettempdir(), f"rrd_test_render_{frame}.png"
        )
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(scene.render.filepath)
        pixels = list(image.pixels)
        bpy.data.images.remove(image)
        reds, greens, blues = pixels[0::4], pixels[1::4], pixels[2::4]
        # Compare against the corner, not against zero: the dark world is not
        # black once the view transform has had its way with it.
        background = max(reds[0], greens[0], blues[0])
        levels = [max(rgb) for rgb in zip(reds, greens, blues)]
        lit = sum(1 for level in levels if level > background + 0.08)
        return max(levels) - background, lit

    brightest_first, lit_first = render_at(1)
    brightest_last, lit_last = render_at(scene.frame_end)

    # The black-render regression: a Material socket that silently fails to
    # set leaves the material unused, Blender purges it, and every frame
    # renders as background.
    check("the render is not just background", brightest_last > 0.1,
          f"brightest pixel is {brightest_last:.4f} above the background")
    check("more is visible at the end than at the start", lit_last > lit_first,
          (lit_first, lit_last))
    check("something is already visible on the first frame", lit_first > 0, lit_first)


def main(argv):
    which = (argv[0] if argv else "all").lower()
    with tempfile.TemporaryDirectory() as tmp:
        from rerun_importer import deps

        if not deps.ensure_on_path():
            print("FATAL: the Rerun SDK is not available to this Blender.")
            print("       run `make wheels`, or press Install Rerun SDK in the add-on.")
            return 2
        path = write_fixture(os.path.join(tmp, "fixture.rrd"))
        print(f"fixture: {path} ({os.path.getsize(path):,} bytes)")
        try:
            if which in ("reader", "all"):
                test_broadcast()
                test_reader(path)
            if which in ("build", "all"):
                test_build(path)
        except Exception:
            traceback.print_exc()
            FAILURES.append("uncaught exception")

    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print(f"  FAILED {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    sys.exit(main(args))
