"""Headless conversion: .rrd -> .blend, without opening Blender's UI.

    blender -b -P tools/rrd2blend.py -- flight.rrd -o flight.blend --fps 30

Handy for batch conversion and for CI; the add-on and this script share every
line of the actual conversion.
"""

from __future__ import annotations

import argparse
import os
import site
import sys


def _bootstrap():
    """Make the add-on importable and its dependencies visible."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    for extra in os.environ.get("RRD_DEPS", "").split(os.pathsep):
        if extra:
            site.addsitedir(extra)
    try:
        import rerun  # noqa: F401
    except ImportError:
        from rerun_importer import deps

        deps.ensure_on_path()


def main(argv):
    parser = argparse.ArgumentParser(prog="rrd2blend", description=__doc__)
    parser.add_argument("rrd")
    parser.add_argument("-o", "--output", help="write a .blend here")
    parser.add_argument("--render", help="also render one frame to this PNG")
    parser.add_argument("--render-frame", type=int, default=0)
    parser.add_argument("--timeline", default="")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--trail", type=float, default=0.0,
                        help="frames a point stays visible (0 = accumulate forever)")
    parser.add_argument("--max-points", type=int, default=2_000_000)
    parser.add_argument("--radius-scale", type=float, default=1.0)
    parser.add_argument("--keyframe-stride", type=int, default=1)
    parser.add_argument("--scalars", action="store_true", help="import Scalars plots")
    parser.add_argument("--no-camera", action="store_true")
    parser.add_argument("--include", action="append", default=[],
                        help="entity path prefix to keep (repeatable)")
    parser.add_argument("--list", action="store_true",
                        help="print the recording's timelines and entities, build nothing")
    args = parser.parse_args(argv)

    import bpy

    # read_factory_settings reloads Blender's scripts, which drops sys.path
    # entries and unloads modules — so reset first, bootstrap after.
    if not args.list:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    _bootstrap()

    from rerun_importer import importer, rrd_reader

    if args.list:
        scene = rrd_reader.probe(args.rrd)
        print("timelines:")
        for timeline in scene.timelines:
            print(f"  {timeline.label()}  [{timeline.kind}]")
        default = rrd_reader.default_timeline(scene.timelines)
        # A recording can be entirely static (everything logged with no time),
        # in which case there is no timeline to animate along.
        print(f"default: {default.name if default else '(none — static recording)'}")
        full = rrd_reader.read(args.rrd, timeline=args.timeline or None, max_points=1)
        print(f"contents: {full.summary()}")
        for entity in full.all_entities():
            print(f"  {entity.path}  ({type(entity).__name__})")
        if full.skipped:
            print(f"unsupported archetypes: {full.skipped}")
        return 0

    options = importer.Options(
        timeline=args.timeline,
        fps=args.fps,
        speed=args.speed,
        trail_frames=args.trail,
        max_points=args.max_points,
        radius_scale=args.radius_scale,
        keyframe_stride=args.keyframe_stride,
        import_scalars=args.scalars,
        add_framing_camera=not args.no_camera,
        include=args.include,
    )
    report = importer.import_rrd(args.rrd, options)
    print(f"[rrd2blend] {report.describe()}")
    if report.skipped:
        print(f"[rrd2blend] unsupported archetypes: {report.skipped}")

    if args.output:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args.output))
        print(f"[rrd2blend] wrote {args.output}")
    if args.render:
        scene = bpy.context.scene
        scene.frame_set(args.render_frame or scene.frame_end)
        scene.render.filepath = os.path.abspath(args.render)
        scene.render.image_settings.file_format = "PNG"
        scene.render.resolution_x, scene.render.resolution_y = 1280, 720
        if scene.render.engine == "CYCLES":
            scene.cycles.samples = 32
        bpy.ops.render.render(write_still=True)
        print(f"[rrd2blend] rendered {args.render}")
    return 0


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    sys.exit(main(argv))
