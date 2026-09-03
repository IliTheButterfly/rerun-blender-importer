"""Rerun (.rrd) importer for Blender.

Turns a Rerun recording into a normal Blender scene — animated point clouds,
posed entities, trajectories, boxes, meshes and cameras — so it can be lit,
composited, and flown through with a Blender camera.

See ``operators.py`` for the UI, ``rrd_reader.py`` for the read path, and
``scene_builder.py``/``nodegroups.py`` for how time and colour survive the
trip.
"""

from __future__ import annotations

import bpy

from . import deps, operators

bl_info = {
    "name": "Rerun (.rrd) Importer",
    "author": "IliTheButterfly",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > Rerun Recording (.rrd)",
    "description": "Import Rerun recordings as animated Blender scenes",
    "category": "Import-Export",
}


def register():
    # Best-effort: if the SDK is already vendored or installed, importing an
    # .rrd works with no further setup; if not, the panel offers to fetch it.
    try:
        deps.ensure_on_path()
    except Exception as exc:  # never block registration on this
        print(f"[rerun] dependency probe failed: {exc}")

    for cls in operators.CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(operators.menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(operators.menu_func_import)
    for cls in reversed(operators.CLASSES):
        bpy.utils.unregister_class(cls)
