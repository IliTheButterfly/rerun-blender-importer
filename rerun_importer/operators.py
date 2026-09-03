"""Operators and panels: the add-on's actual user interface."""

from __future__ import annotations

import math
import os
import time

import bpy
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
    IntProperty, StringProperty,
)
from bpy.types import Operator, Panel
from bpy_extras.io_utils import ImportHelper

from . import deps

# Probing an .rrd streams its chunk index, so results are cached: a draw
# callback runs many times per second and must not re-read the file.
_probe_cache: dict = {}
_PROBE_TTL = 60.0

AUTO_TIMELINE = "AUTO"


def _probe(path: str):
    if not path or not os.path.isfile(path):
        return None
    key = (path, os.path.getmtime(path))
    hit = _probe_cache.get(key)
    if hit and time.monotonic() - hit[0] < _PROBE_TTL:
        return hit[1]
    if not deps.ensure_on_path():
        return None
    try:
        from . import rrd_reader

        scene = rrd_reader.probe(path)
    except Exception as exc:  # a truncated or foreign file must not break the UI
        print(f"[rerun] could not probe {path}: {exc}")
        return None
    _probe_cache.clear()
    _probe_cache[key] = (time.monotonic(), scene)
    return scene


def _timeline_items(self, context):
    # An empty identifier makes Blender warn that the stored value matches no
    # enum item, so "auto" is a real item rather than a blank one.
    items = [(AUTO_TIMELINE, "Auto", "Pick the most useful timeline in the recording")]
    scene = _probe(getattr(self, "filepath", ""))
    if scene:
        for timeline in scene.timelines:
            items.append((timeline.name, timeline.label(), f"{timeline.kind} timeline"))
    return items


class RRD_OT_install_dependencies(Operator):
    """Download the Rerun SDK into this add-on so it can read .rrd files"""

    bl_idname = "rrd.install_dependencies"
    bl_label = "Install Rerun SDK"
    bl_options = {"REGISTER", "INTERNAL"}

    upgrade: BoolProperty(name="Upgrade", default=False)  # type: ignore

    def execute(self, context):
        self.report({"INFO"}, "Downloading the Rerun SDK — this can take a minute…")
        try:
            deps.install(upgrade=self.upgrade)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc).splitlines()[-1][:300])
            print(f"[rerun] dependency install failed:\n{exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Installed {deps.versions()}")
        return {"FINISHED"}


class RRD_OT_import(Operator, ImportHelper):
    """Import a Rerun recording as an animated Blender scene"""

    bl_idname = "import_scene.rrd"
    bl_label = "Import Rerun Recording"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".rrd"
    filter_glob: StringProperty(default="*.rrd", options={"HIDDEN"})  # type: ignore
    files: CollectionProperty(type=bpy.types.OperatorFileListElement,
                              options={"HIDDEN", "SKIP_SAVE"})  # type: ignore
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})  # type: ignore

    timeline: EnumProperty(
        name="Timeline",
        description="Which of the recording's timelines becomes Blender's timeline",
        items=_timeline_items,
    )  # type: ignore
    fps: FloatProperty(
        name="FPS", default=30.0, min=1.0, max=240.0,
        description="Blender frames per second of recorded time",
    )  # type: ignore
    speed: FloatProperty(
        name="Time Scale", default=1.0, min=0.001, soft_max=100.0,
        description="Seconds of recording per second of animation. "
                    "Above 1 compresses a long flight into a short shot",
    )  # type: ignore
    set_scene_range: BoolProperty(name="Set Frame Range", default=True)  # type: ignore
    set_scene_fps: BoolProperty(name="Set Scene FPS", default=True)  # type: ignore

    animate: BoolProperty(
        name="Animate Over Time", default=True,
        description="Reveal data as the recording logged it. Off shows everything at once",
    )  # type: ignore
    trail_frames: FloatProperty(
        name="Trail", default=0.0, min=0.0, soft_max=250.0,
        description="How many frames a point stays visible. 0 accumulates forever, "
                    "which is what a SLAM map wants; a small value gives a sensor sweep",
    )  # type: ignore
    keyframe_stride: IntProperty(
        name="Keyframe Stride", default=1, min=1, soft_max=20,
        description="Keep every Nth transform sample. Raise it if a long flight "
                    "makes the scene sluggish",
    )  # type: ignore

    max_points: IntProperty(
        name="Point Budget", default=2_000_000, min=0, soft_max=20_000_000,
        description="Cap per point cloud, randomly subsampled. 0 means no cap",
    )  # type: ignore
    radius_scale: FloatProperty(name="Radius Scale", default=1.0, min=0.0, soft_max=20.0)  # type: ignore
    default_radius: FloatProperty(
        name="Default Radius", default=0.02, min=0.0, soft_max=1.0,
        description="Point radius when the recording did not log one",
    )  # type: ignore
    line_radius: FloatProperty(name="Line Radius", default=0.05, min=0.0, soft_max=5.0)  # type: ignore

    emissive: BoolProperty(
        name="Emissive Colours", default=True,
        description="Make logged colours glow, the way the Rerun viewer draws them. "
                    "Off gives lit, shadeable surfaces",
    )  # type: ignore
    emission_strength: FloatProperty(name="Emission", default=1.0, min=0.0, soft_max=10.0)  # type: ignore
    dark_world: BoolProperty(name="Dark Background", default=True)  # type: ignore

    import_clouds: BoolProperty(name="Point Clouds", default=True)  # type: ignore
    import_lines: BoolProperty(name="Line Strips", default=True)  # type: ignore
    import_boxes: BoolProperty(name="Boxes", default=True)  # type: ignore
    import_meshes: BoolProperty(name="Meshes & Assets", default=True)  # type: ignore
    import_cameras: BoolProperty(name="Pinhole Cameras", default=True)  # type: ignore
    import_scalars: BoolProperty(
        name="Scalar Plots", default=False,
        description="Import Scalars as keyframed custom properties, usable as drivers",
    )  # type: ignore
    add_framing_camera: BoolProperty(
        name="Add Framing Camera", default=True,
        description="Add a camera already aimed at the data, as a starting point",
    )  # type: ignore
    include: StringProperty(
        name="Only Entities",
        description="Comma-separated entity path prefixes to import, e.g. /world/map",
    )  # type: ignore

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        if not deps.available():
            box = layout.box()
            box.label(text="Rerun SDK not installed", icon="ERROR")
            box.label(text="Reading .rrd needs Rerun's own reader.")
            box.operator(RRD_OT_install_dependencies.bl_idname, icon="IMPORT")
            return

        scene = _probe(self.filepath)
        if scene:
            info = layout.box()
            info.label(text=os.path.basename(self.filepath), icon="FILE_CACHE")
            for timeline in scene.timelines[:6]:
                info.label(text=timeline.label(), icon="TIME")

        header, body = layout.panel("rrd_time", default_closed=False)
        header.label(text="Time")
        if body:
            body.prop(self, "timeline")
            body.prop(self, "fps")
            body.prop(self, "speed")
            body.prop(self, "animate")
            body.prop(self, "trail_frames")
            body.prop(self, "keyframe_stride")
            body.prop(self, "set_scene_range")
            body.prop(self, "set_scene_fps")

        header, body = layout.panel("rrd_look", default_closed=False)
        header.label(text="Look")
        if body:
            body.prop(self, "emissive")
            sub = body.row()
            sub.enabled = self.emissive
            sub.prop(self, "emission_strength")
            body.prop(self, "dark_world")
            body.prop(self, "radius_scale")
            body.prop(self, "default_radius")
            body.prop(self, "line_radius")

        header, body = layout.panel("rrd_contents", default_closed=True)
        header.label(text="Contents")
        if body:
            body.prop(self, "import_clouds")
            body.prop(self, "import_lines")
            body.prop(self, "import_boxes")
            body.prop(self, "import_meshes")
            body.prop(self, "import_cameras")
            body.prop(self, "import_scalars")
            body.prop(self, "add_framing_camera")
            body.prop(self, "max_points")
            body.prop(self, "include")

    def execute(self, context):
        if not deps.ensure_on_path():
            self.report({"ERROR"}, "Rerun SDK is not installed — see the import panel")
            return {"CANCELLED"}

        from . import importer

        paths = [os.path.join(self.directory, f.name) for f in self.files if f.name] or [self.filepath]
        options = importer.Options(
            timeline="" if self.timeline == AUTO_TIMELINE else self.timeline,
            fps=self.fps,
            speed=self.speed,
            set_scene_range=self.set_scene_range,
            set_scene_fps=self.set_scene_fps,
            animate=self.animate,
            trail_frames=self.trail_frames,
            keyframe_stride=self.keyframe_stride,
            max_points=self.max_points,
            default_radius=self.default_radius,
            radius_scale=self.radius_scale,
            line_radius=self.line_radius,
            emissive=self.emissive,
            emission_strength=self.emission_strength,
            dark_world=self.dark_world,
            import_clouds=self.import_clouds,
            import_lines=self.import_lines,
            import_boxes=self.import_boxes,
            import_meshes=self.import_meshes,
            import_cameras=self.import_cameras,
            import_scalars=self.import_scalars,
            add_framing_camera=self.add_framing_camera,
            include=[p.strip() for p in self.include.split(",") if p.strip()],
        )

        messages = []
        for path in paths:
            try:
                report = importer.import_rrd(path, options, context=context)
            except Exception as exc:
                self.report({"ERROR"}, f"{os.path.basename(path)}: {exc}")
                print(f"[rerun] import of {path} failed")
                import traceback

                traceback.print_exc()
                return {"CANCELLED"}
            messages.append(f"{os.path.basename(path)}: {report.describe()}")
            if report.skipped:
                print(f"[rerun] unsupported archetypes in {path}: {report.skipped}")
        self.report({"INFO"}, " | ".join(messages))
        return {"FINISHED"}


class RRD_OT_add_chase_camera(Operator):
    """Add a camera that follows the selected object and looks at it"""

    bl_idname = "rrd.add_chase_camera"
    bl_label = "Add Chase Camera"
    bl_options = {"REGISTER", "UNDO"}

    distance: FloatProperty(name="Distance", default=12.0, min=0.1, soft_max=200.0)  # type: ignore
    height: FloatProperty(name="Height", default=6.0, soft_min=-50.0, soft_max=100.0)  # type: ignore
    bearing: FloatProperty(
        name="Bearing", default=math.radians(-135.0), subtype="ANGLE",
        description="Where the camera sits relative to the target, around Z",
    )  # type: ignore
    inherit_rotation: BoolProperty(
        name="Follow Heading", default=False,
        description="Rotate with the target, so the shot stays behind it. "
                    "Off keeps a fixed bearing, which is steadier over a whole flight",
    )  # type: ignore
    make_active: BoolProperty(name="Make Active Camera", default=True)  # type: ignore

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        target = context.active_object
        collection = target.users_collection[0] if target.users_collection else context.scene.collection

        dolly = bpy.data.objects.new(f"{target.name} Dolly", None)
        dolly.empty_display_type = "SPHERE"
        dolly.empty_display_size = 0.2
        collection.objects.link(dolly)
        copy_loc = dolly.constraints.new("COPY_LOCATION")
        copy_loc.target = target
        if self.inherit_rotation:
            copy_rot = dolly.constraints.new("COPY_ROTATION")
            copy_rot.target = target

        camera_data = bpy.data.cameras.new(f"{target.name} Chase Cam")
        camera_data.clip_start = 0.05
        camera_data.clip_end = max(1000.0, self.distance * 50.0)
        camera = bpy.data.objects.new(camera_data.name, camera_data)
        collection.objects.link(camera)
        camera.parent = dolly
        camera.location = (
            math.cos(self.bearing) * self.distance,
            math.sin(self.bearing) * self.distance,
            self.height,
        )
        track = camera.constraints.new("TRACK_TO")
        track.target = dolly
        track.track_axis = "TRACK_NEGATIVE_Z"
        track.up_axis = "UP_Y"

        if self.make_active:
            context.scene.camera = camera
        self.report({"INFO"}, f"{camera.name} now follows {target.name}")
        return {"FINISHED"}


class RRD_OT_set_trail(Operator):
    """Set the trail length on every selected Rerun object at once"""

    bl_idname = "rrd.set_trail"
    bl_label = "Set Trail"
    bl_options = {"REGISTER", "UNDO"}

    trail: FloatProperty(name="Trail Frames", default=0.0, min=0.0, soft_max=250.0)  # type: ignore
    animate: BoolProperty(name="Animate Over Time", default=True)  # type: ignore

    def execute(self, context):
        from .scene_builder import _set_modifier_inputs

        touched = 0
        for obj in context.selected_objects:
            for mod in obj.modifiers:
                if mod.type != "NODES" or not mod.node_group:
                    continue
                if not mod.node_group.name.startswith("Rerun"):
                    continue
                _set_modifier_inputs(mod, {"Trail": self.trail, "Animate": self.animate})
                touched += 1
        if not touched:
            self.report({"WARNING"}, "No imported Rerun objects selected")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Updated {touched} object(s)")
        return {"FINISHED"}


class RRD_PT_panel(Panel):
    """Sidebar panel: the few things worth reaching for after an import"""

    bl_label = "Rerun"
    bl_idname = "RRD_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rerun"

    def draw(self, context):
        layout = self.layout
        if not deps.available():
            box = layout.box()
            box.label(text="Rerun SDK missing", icon="ERROR")
            box.operator(RRD_OT_install_dependencies.bl_idname, icon="IMPORT")
        else:
            layout.label(text=deps.versions(), icon="CHECKMARK")

        layout.operator(RRD_OT_import.bl_idname, text="Import .rrd", icon="IMPORT")

        column = layout.column(align=True)
        column.label(text="Camera")
        column.operator(RRD_OT_add_chase_camera.bl_idname, icon="CON_CAMERASOLVE")

        column = layout.column(align=True)
        column.label(text="Selected objects")
        column.operator(RRD_OT_set_trail.bl_idname, icon="TRACKING")


def menu_func_import(self, context):
    self.layout.operator(RRD_OT_import.bl_idname, text="Rerun Recording (.rrd)")


CLASSES = (
    RRD_OT_install_dependencies,
    RRD_OT_import,
    RRD_OT_add_chase_camera,
    RRD_OT_set_trail,
    RRD_PT_panel,
)
