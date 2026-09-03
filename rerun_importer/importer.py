"""Orchestration: read an .rrd, then build the whole Blender scene from it."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

import bpy
import numpy as np

from . import ir, nodegroups, rrd_reader, scene_builder
from .scene_builder import FrameMap, Hierarchy


@dataclass
class Options:
    """Everything the import dialog can change."""

    timeline: str = ""
    fps: float = 30.0
    frame_start: int = 1
    speed: float = 1.0
    set_scene_range: bool = True
    set_scene_fps: bool = True

    animate: bool = True
    trail_frames: float = 0.0
    keyframe_stride: int = 1

    max_points: int = 2_000_000
    default_radius: float = 0.02
    radius_scale: float = 1.0
    min_radius: float = 0.005
    line_radius: float = 0.05
    line_profile_resolution: int = 6

    emissive: bool = True
    emission_strength: float = 1.0
    dark_world: bool = True
    clip_end: float = 10_000.0

    import_clouds: bool = True
    import_lines: bool = True
    import_boxes: bool = True
    import_meshes: bool = True
    import_cameras: bool = True
    import_scalars: bool = False
    add_framing_camera: bool = True

    include: list = field(default_factory=list)


@dataclass
class Report:
    """What actually got built, for the operator to report back."""

    clouds: int = 0
    points: int = 0
    lines: int = 0
    boxes: int = 0
    meshes: int = 0
    assets: int = 0
    cameras: int = 0
    scalars: int = 0
    keyframes: int = 0
    frame_end: int = 1
    truncated: bool = False
    skipped: dict = field(default_factory=dict)

    def describe(self) -> str:
        bits = []
        if self.clouds:
            bits.append(f"{self.points:,} points in {self.clouds} cloud(s)")
        if self.lines:
            bits.append(f"{self.lines} line entity(ies)")
        if self.boxes:
            bits.append(f"{self.boxes} box entity(ies)")
        if self.meshes or self.assets:
            bits.append(f"{self.meshes + self.assets} mesh/asset(s)")
        if self.cameras:
            bits.append(f"{self.cameras} camera(s)")
        if self.scalars:
            bits.append(f"{self.scalars} scalar series")
        if self.keyframes:
            bits.append(f"{self.keyframes:,} keyframes")
        text = ", ".join(bits) or "nothing importable"
        if self.truncated:
            text += " (point budget hit — some points were dropped)"
        return f"{text}; frames 1-{self.frame_end}"


def import_rrd(path: str, options: Options, context=None) -> Report:
    """Read ``path`` and build it into the current scene."""
    scene = rrd_reader.read(
        path,
        timeline=options.timeline or None,
        max_points=int(options.max_points),
        include=options.include or None,
    )
    return build(scene, options, context=context)


def build(scene: ir.Scene, options: Options, context=None) -> Report:
    context = context or bpy.context
    report = Report(skipped=dict(scene.skipped))

    name = os.path.basename(scene.path) or "rerun"
    root = bpy.data.collections.new(f"Rerun: {name}")
    context.scene.collection.children.link(root)

    frames = FrameMap(scene.timeline, options.fps, options.frame_start, options.speed)
    hierarchy = Hierarchy(root)

    material = nodegroups.rerun_material(
        "Rerun Vertex Colour",
        emissive=options.emissive,
        strength=options.emission_strength,
    )

    # Transforms first: everything else parents into the hierarchy they define.
    for series in scene.transforms:
        obj = hierarchy.empty_for(series.path)
        if obj is None:
            continue
        obj.empty_display_type = "ARROWS"
        report.keyframes += scene_builder.animate_transform(obj, series, frames, options)

    if options.import_clouds:
        for cloud in scene.clouds:
            if not cloud.count:
                continue
            obj = scene_builder.build_point_cloud(cloud, frames, material, options)
            hierarchy.replace(cloud.path, obj)
            report.clouds += 1
            report.points += cloud.count
            if options.max_points and cloud.rows and cloud.count >= options.max_points:
                report.truncated = True

    if options.import_lines:
        for lines in scene.lines:
            obj = scene_builder.build_line_strips(lines, frames, material, options)
            if obj is not None:
                hierarchy.replace(lines.path, obj)
                report.lines += 1

    if options.import_boxes:
        for boxes in scene.boxes:
            obj = scene_builder.build_boxes(boxes, frames, material, options)
            if obj is not None:
                hierarchy.replace(boxes.path, obj)
                report.boxes += 1

    if options.import_meshes:
        for tri in scene.meshes:
            obj = scene_builder.build_mesh(tri, material, options)
            if obj is not None:
                hierarchy.replace(tri.path, obj)
                report.meshes += 1
        for asset in scene.assets:
            if _import_asset(asset, hierarchy, root):
                report.assets += 1

    if options.import_cameras:
        for pinhole in scene.pinholes:
            obj = scene_builder.build_camera(pinhole, options)
            hierarchy.attach(pinhole.path, obj)
            report.cameras += 1

    if options.import_scalars:
        plots = bpy.data.collections.new(f"Rerun plots: {name}")
        root.children.link(plots)
        for series in scene.scalars:
            obj = bpy.data.objects.new(series.path.strip("/").replace("/", "."), None)
            obj.empty_display_size = 0.05
            plots.objects.link(obj)
            scene_builder.animate_scalars(obj, series, frames, options)
            report.scalars += 1

    if options.add_framing_camera:
        cam = scene_builder.add_framing_camera(scene, root, options)
        if context.scene.camera is None:
            context.scene.camera = cam
        report.cameras += 1

    report.frame_end = frames.frame_end
    if options.set_scene_range:
        context.scene.frame_start = options.frame_start
        context.scene.frame_end = max(options.frame_start, report.frame_end)
    if options.set_scene_fps:
        context.scene.render.fps = int(round(options.fps))
    if options.dark_world:
        _dark_world(context)

    _set_viewport_clip(options.clip_end)
    return report


def _import_asset(asset: ir.Asset, hierarchy: Hierarchy, collection) -> bool:
    """Asset3D is a whole model file inside the recording — unpack and import."""
    suffix = {
        "model/gltf-binary": ".glb",
        "model/gltf+json": ".gltf",
        "model/obj": ".obj",
        "model/stl": ".stl",
        "application/octet-stream": ".glb",
    }.get(asset.media_type.split(";")[0].strip(), "")
    if not suffix:
        head = asset.blob[:5]
        suffix = ".glb" if head[:4] == b"glTF" else ".ply" if head[:3] == b"ply" else ".obj"

    before = set(bpy.data.objects)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(asset.blob)
        temp_path = handle.name
    try:
        if suffix in (".glb", ".gltf"):
            bpy.ops.import_scene.gltf(filepath=temp_path)
        elif suffix == ".obj":
            bpy.ops.wm.obj_import(filepath=temp_path)
        elif suffix == ".stl":
            bpy.ops.wm.stl_import(filepath=temp_path)
        elif suffix == ".ply":
            bpy.ops.wm.ply_import(filepath=temp_path)
        else:
            return False
    except Exception:
        return False
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    parent = hierarchy.empty_for(asset.path)
    for obj in set(bpy.data.objects) - before:
        for other in list(obj.users_collection):
            other.objects.unlink(obj)
        collection.objects.link(obj)
        if obj.parent is None and parent is not None and obj is not parent:
            obj.parent = parent
    return True


def _dark_world(context):
    """Rerun's viewport is near-black; match it so colours read the same."""
    world = context.scene.world
    if world is None:
        world = bpy.data.worlds.new("Rerun World")
        context.scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (0.012, 0.014, 0.017, 1.0)
        background.inputs["Strength"].default_value = 1.0


def _set_viewport_clip(clip_end: float):
    """A 100 m site is invisible behind Blender's default 100 m clip plane."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.clip_end = max(space.clip_end, clip_end)
