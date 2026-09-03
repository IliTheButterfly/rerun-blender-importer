"""Turn an :class:`ir.Scene` into Blender data.

Design decisions worth knowing before editing this:

* **Entity paths become a parent hierarchy of empties.**  Rerun resolves a
  transform at the cursor time and applies it to everything below it, which is
  exactly what Blender parenting does — so a cloud logged in a sensor frame
  moves with the drone, and a cloud logged in world coordinates (no transform
  ancestor) stays put.  Getting this wrong is the classic "the map slides with
  the drone" bug, so the hierarchy is reproduced rather than pre-multiplied.
* **Nothing is baked per frame.**  Time lives in a point attribute plus the
  geometry nodes in :mod:`nodegroups`.
* **Colours are converted sRGB -> linear** on the way in.  Rerun logs 8-bit
  sRGB; Blender's shaders work in linear, so skipping this makes everything
  look washed out.
"""

from __future__ import annotations

import math

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

from . import ir, nodegroups


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def srgb_to_linear(rgba_u8: np.ndarray) -> np.ndarray:
    """8-bit sRGB -> linear float, the transfer function Blender expects."""
    c = np.asarray(rgba_u8, dtype=np.float32) / 255.0
    rgb = c[..., :3]
    linear = np.where(
        rgb <= 0.04045, rgb / 12.92, np.power((rgb + 0.055) / 1.055, 2.4)
    ).astype(np.float32)
    out = np.empty_like(c)
    out[..., :3] = linear
    out[..., 3] = c[..., 3] if c.shape[-1] > 3 else 1.0
    return out


class FrameMap:
    """Map rrd timeline values to Blender frame numbers."""

    def __init__(self, timeline, fps: float, frame_start: int = 1, speed: float = 1.0):
        self.timeline = timeline
        self.fps = float(fps)
        self.frame_start = int(frame_start)
        self.speed = float(speed) or 1.0
        self.t_zero = timeline.t_min if timeline else 0
        self.temporal = bool(timeline and timeline.is_temporal)

    def __call__(self, t):
        t = np.asarray(t, dtype=np.float64)
        if self.temporal:
            frames = (t - self.t_zero) / 1e9 * self.fps / self.speed
        else:
            frames = (t - self.t_zero) / self.speed
        return frames + self.frame_start

    @property
    def frame_end(self) -> int:
        if not self.timeline:
            return self.frame_start
        return int(math.ceil(float(self(self.timeline.t_max)))) or self.frame_start


def _set_attribute(mesh, name, kind, domain, values):
    attr = mesh.attributes.get(name)
    if attr is None or attr.data_type != kind or attr.domain != domain:
        if attr is not None:
            mesh.attributes.remove(attr)
        attr = mesh.attributes.new(name=name, type=kind, domain=domain)
    field = {
        "FLOAT": "value",
        "INT": "value",
        "FLOAT_VECTOR": "vector",
        "FLOAT_COLOR": "color",
        "QUATERNION": "value",
    }[kind]
    attr.data.foreach_set(field, np.ascontiguousarray(values).ravel())
    return attr


# ---------------------------------------------------------------------------
# hierarchy
# ---------------------------------------------------------------------------


class Hierarchy:
    """Entity path -> Blender object, creating empties for intermediate nodes."""

    def __init__(self, collection, prefix=""):
        self.collection = collection
        self.prefix = prefix
        self.objects: dict = {}

    def _parts(self, path: str):
        return [p for p in path.strip("/").split("/") if p]

    def empty_for(self, path: str) -> "bpy.types.Object | None":
        """Object for ``path``, creating plain empties along the way."""
        parts = self._parts(path)
        if not parts:
            return None
        parent = None
        walked = ""
        for part in parts:
            walked = f"{walked}/{part}"
            obj = self.objects.get(walked)
            if obj is None:
                obj = bpy.data.objects.new(f"{self.prefix}{part}", None)
                obj.empty_display_size = 0.25
                obj.empty_display_type = "PLAIN_AXES"
                self.collection.objects.link(obj)
                if parent is not None:
                    obj.parent = parent
                self.objects[walked] = obj
            parent = obj
        return parent

    def attach(self, path: str, obj: bpy.types.Object):
        """Link ``obj`` into the scene at ``path``, parented to its ancestor."""
        parts = self._parts(path)
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else ""
        parent = self.empty_for(parent_path) if parent_path else None
        self.collection.objects.link(obj)
        if parent is not None:
            obj.parent = parent
        self.objects["/" + "/".join(parts)] = obj
        return obj

    def replace(self, path: str, obj: bpy.types.Object):
        """Use ``obj`` where an empty already stands (keeping its children)."""
        key = "/" + "/".join(self._parts(path))
        old = self.objects.get(key)
        if old is not None and old.type == "EMPTY":
            children = list(old.children)
            obj.parent = old.parent
            obj.matrix_parent_inverse = old.matrix_parent_inverse.copy()
            self.collection.objects.link(obj)
            for child in children:
                child.parent = obj
            bpy.data.objects.remove(old, do_unlink=True)
            self.objects[key] = obj
            return obj
        return self.attach(path, obj)


# ---------------------------------------------------------------------------
# builders per archetype
# ---------------------------------------------------------------------------


def build_point_cloud(cloud: ir.PointCloud, frames: FrameMap, material, options):
    n = cloud.count
    mesh = bpy.data.meshes.new(f"{cloud.name}_points")
    mesh.vertices.add(n)
    mesh.vertices.foreach_set("co", np.ascontiguousarray(cloud.positions, np.float32).ravel())
    mesh.update()

    _set_attribute(mesh, nodegroups.BIRTH_ATTR, "FLOAT", "POINT",
                   frames(cloud.birth).astype(np.float32))
    if cloud.colors is not None:
        _set_attribute(mesh, nodegroups.COLOR_ATTR, "FLOAT_COLOR", "POINT",
                       srgb_to_linear(cloud.colors))
    radii = cloud.radii
    if radii is None:
        radii = np.full(n, options.default_radius, np.float32)
    _set_attribute(mesh, nodegroups.RADIUS_ATTR, "FLOAT", "POINT",
                   np.asarray(radii, np.float32))

    obj = bpy.data.objects.new(cloud.name, mesh)
    mod = obj.modifiers.new("Rerun Points", "NODES")
    mod.node_group = nodegroups.point_cloud_group()
    _set_modifier_inputs(mod, {
        "Animate": options.animate,
        "Trail": options.trail_frames,
        "Material": material,
        "Radius Scale": options.radius_scale,
        "Min Radius": options.min_radius,
    })
    return obj


def build_line_strips(lines: ir.LineStrips, frames: FrameMap, material, options):
    verts = []
    edges = []
    birth = []
    colors = []
    offset = 0
    for i, strip in enumerate(lines.strips):
        if len(strip) < 1:
            continue
        verts.append(np.asarray(strip, np.float32))
        n = len(strip)
        if n > 1:
            idx = np.arange(offset, offset + n - 1, dtype=np.int32)
            edges.append(np.stack([idx, idx + 1], axis=1))
        b = lines.birth[i] if i < len(lines.birth) else np.zeros(n, np.int64)
        birth.append(np.asarray(b, np.int64))
        rgba = lines.colors[i] if i < len(lines.colors) else np.array([255, 255, 255, 255], np.uint8)
        colors.append(np.repeat(np.asarray(rgba, np.uint8)[None, :], n, axis=0))
        offset += n
    if not verts:
        return None

    positions = np.concatenate(verts)
    mesh = bpy.data.meshes.new(f"{lines.name}_lines")
    mesh.vertices.add(len(positions))
    mesh.vertices.foreach_set("co", np.ascontiguousarray(positions).ravel())
    if edges:
        all_edges = np.concatenate(edges)
        mesh.edges.add(len(all_edges))
        mesh.edges.foreach_set("vertices", np.ascontiguousarray(all_edges, np.int32).ravel())
    mesh.update()

    _set_attribute(mesh, nodegroups.BIRTH_ATTR, "FLOAT", "POINT",
                   frames(np.concatenate(birth)).astype(np.float32))
    _set_attribute(mesh, nodegroups.COLOR_ATTR, "FLOAT_COLOR", "POINT",
                   srgb_to_linear(np.concatenate(colors)))

    obj = bpy.data.objects.new(lines.name, mesh)
    mod = obj.modifiers.new("Rerun Lines", "NODES")
    mod.node_group = nodegroups.line_strips_group()
    radius = float(np.median(lines.radii)) if lines.radii else options.line_radius
    _set_modifier_inputs(mod, {
        "Animate": options.animate,
        "Trail": options.trail_frames,
        "Material": material,
        "Radius": max(radius, 1e-4),
        "Profile Resolution": options.line_profile_resolution,
    })
    return obj


def build_boxes(boxes: ir.Boxes, frames: FrameMap, material, options):
    n = 0 if boxes.centers is None else len(boxes.centers)
    if not n:
        return None
    mesh = bpy.data.meshes.new(f"{boxes.name}_boxes")
    mesh.vertices.add(n)
    mesh.vertices.foreach_set("co", np.ascontiguousarray(boxes.centers, np.float32).ravel())
    mesh.update()

    _set_attribute(mesh, nodegroups.BIRTH_ATTR, "FLOAT", "POINT",
                   frames(boxes.birth).astype(np.float32))
    _set_attribute(mesh, nodegroups.HALF_SIZE_ATTR, "FLOAT_VECTOR", "POINT",
                   np.asarray(boxes.half_sizes, np.float32))
    if boxes.colors is not None:
        _set_attribute(mesh, nodegroups.COLOR_ATTR, "FLOAT_COLOR", "POINT",
                       srgb_to_linear(boxes.colors))
    if boxes.quaternions is not None:
        # Rerun stores xyzw; Blender quaternion attributes are wxyz.
        q = np.asarray(boxes.quaternions, np.float32)
        wxyz = np.stack([q[:, 3], q[:, 0], q[:, 1], q[:, 2]], axis=1)
        try:
            _set_attribute(mesh, nodegroups.ROTATION_ATTR, "QUATERNION", "POINT", wxyz)
        except (KeyError, RuntimeError):
            pass

    obj = bpy.data.objects.new(boxes.name, mesh)
    mod = obj.modifiers.new("Rerun Boxes", "NODES")
    mod.node_group = nodegroups.boxes_group()
    _set_modifier_inputs(mod, {
        "Animate": options.animate,
        "Trail": options.trail_frames,
        "Material": material,
    })
    return obj


def build_mesh(tri: ir.TriMesh, material, options):
    if tri.vertices is None or not len(tri.vertices):
        return None
    mesh = bpy.data.meshes.new(f"{tri.name}_mesh")
    verts = np.asarray(tri.vertices, np.float32)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", np.ascontiguousarray(verts).ravel())

    tris = tri.triangles
    if tris is None or not len(tris):
        tris = np.arange(len(verts) - len(verts) % 3, dtype=np.int64).reshape(-1, 3)
    tris = np.asarray(tris, np.int64)
    tris = tris[(tris < len(verts)).all(axis=1)]
    if len(tris):
        mesh.loops.add(len(tris) * 3)
        mesh.polygons.add(len(tris))
        mesh.loops.foreach_set("vertex_index", np.ascontiguousarray(tris, np.int32).ravel())
        mesh.polygons.foreach_set("loop_start", np.arange(0, len(tris) * 3, 3, dtype=np.int32))
        mesh.polygons.foreach_set("loop_total", np.full(len(tris), 3, np.int32))
    mesh.update()
    mesh.validate(verbose=False)

    if tri.colors is not None and len(tri.colors) == len(verts):
        _set_attribute(mesh, nodegroups.COLOR_ATTR, "FLOAT_COLOR", "POINT",
                       srgb_to_linear(tri.colors))
    if tri.uvs is not None and len(tri.uvs) == len(verts) and len(tris):
        uv = mesh.uv_layers.new(name="UVMap")
        per_loop = np.asarray(tri.uvs, np.float32)[tris.ravel()]
        uv.data.foreach_set("uv", np.ascontiguousarray(per_loop).ravel())

    obj = bpy.data.objects.new(tri.name, mesh)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def build_camera(pinhole: ir.Pinhole, options):
    cam = bpy.data.cameras.new(pinhole.name)
    res = pinhole.resolution if pinhole.resolution is not None else np.array([1920.0, 1080.0])
    focal = pinhole.focal if pinhole.focal is not None else np.array([res[0], res[0]])
    cam.sensor_fit = "HORIZONTAL"
    cam.sensor_width = 36.0
    cam.lens = float(focal[0]) / float(res[0]) * cam.sensor_width
    if pinhole.principal is not None and float(res[0]) and float(res[1]):
        cam.shift_x = float(0.5 - pinhole.principal[0] / res[0])
        cam.shift_y = float(pinhole.principal[1] / res[1] - 0.5) * float(res[1]) / float(res[0])
    cam.clip_start = 0.05
    cam.clip_end = max(1000.0, options.clip_end)
    obj = bpy.data.objects.new(pinhole.name, cam)
    # Rerun's pinhole looks down +Z with +Y down; Blender cameras look down -Z
    # with +Y up.  One 180 degrees about X reconciles them.
    obj.matrix_basis = Matrix.Rotation(math.pi, 4, "X")
    return obj


def animate_transform(obj, series: ir.TransformSeries, frames: FrameMap, options):
    """Keyframe an object from a Transform3D series (LINEAR — this is data)."""
    times = np.asarray(series.times)
    if not len(times):
        return 0
    blender_frames = frames(times)

    n = len(times)
    translations = (
        np.asarray(series.translations, np.float64)
        if series.translations is not None
        else np.zeros((n, 3))
    )
    quats = None
    if series.quaternions is not None:
        q = np.asarray(series.quaternions, np.float64)
        quats = [Quaternion((row[3], row[0], row[1], row[2])) for row in q]
    elif series.matrices is not None:
        quats = []
        for m in np.asarray(series.matrices, np.float64):
            # Rerun's mat3x3 is column-major: the flat 9 are three columns.
            mat = Matrix((
                (m[0][0], m[1][0], m[2][0]),
                (m[0][1], m[1][1], m[2][1]),
                (m[0][2], m[1][2], m[2][2]),
            ))
            quats.append(mat.to_quaternion())
    scales = (
        np.asarray(series.scales, np.float64) if series.scales is not None else None
    )

    obj.rotation_mode = "QUATERNION"
    step = max(1, int(options.keyframe_stride))
    written = 0
    previous = None
    for i in range(0, n, step):
        frame = float(blender_frames[i])
        obj.location = Vector(translations[min(i, len(translations) - 1)])
        if quats is not None:
            q = quats[min(i, len(quats) - 1)]
            if previous is not None and q.dot(previous) < 0:
                q = -q  # keep the shortest path; flips read as a spin
            previous = q
            obj.rotation_quaternion = q
        if scales is not None:
            obj.scale = Vector(scales[min(i, len(scales) - 1)])
        obj.keyframe_insert("location", frame=frame)
        if quats is not None:
            obj.keyframe_insert("rotation_quaternion", frame=frame)
        if scales is not None:
            obj.keyframe_insert("scale", frame=frame)
        written += 1

    if obj.animation_data and obj.animation_data.action:
        for fcurve in _action_fcurves(obj.animation_data.action):
            for kp in fcurve.keyframe_points:
                kp.interpolation = "LINEAR"
    return written


def _action_fcurves(action):
    """F-curves of an action, across Blender's slotted-action change in 4.4+."""
    if hasattr(action, "fcurves") and len(action.fcurves):
        return list(action.fcurves)
    curves = []
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for bag in getattr(strip, "channelbags", []):
                curves.extend(bag.fcurves)
    return curves


def animate_scalars(obj, series: ir.Scalars, frames: FrameMap, options):
    """Park a scalar plot on a custom property so it can drive anything."""
    obj["value"] = 0.0
    blender_frames = frames(series.times)
    step = max(1, int(options.keyframe_stride))
    for i in range(0, len(series.values), step):
        obj["value"] = float(series.values[i])
        obj.keyframe_insert('["value"]', frame=float(blender_frames[i]))
    return obj


def _set_modifier_inputs(mod, values: dict):
    """Set geometry-node modifier inputs by interface name.

    Blender 5.0 moved these off the modifier's own ID properties and onto
    ``modifier.properties.inputs[identifier]["value"]``; 4.x wants
    ``modifier[identifier] = value``.  Both are tried, newest first, and a
    socket that cannot be set is reported rather than silently ignored — a
    dropped Material socket leaves the material with no users, which means
    Blender purges it on save and the render comes out black.
    """
    group = mod.node_group
    by_name = {}
    for item in group.interface.items_tree:
        if getattr(item, "item_type", "SOCKET") == "SOCKET" and item.in_out == "INPUT":
            by_name[item.name] = item.identifier

    failed = []
    for name, value in values.items():
        ident = by_name.get(name)
        if ident is None:
            failed.append(name)
            continue
        if not _write_modifier_input(mod, ident, value):
            failed.append(name)
    if failed:
        print(f"[rerun] could not set modifier input(s): {', '.join(failed)}")
    return not failed


def _read_modifier_input(mod, group, name):
    """Read back one modifier input by interface name (None if unreadable)."""
    ident = next(
        (
            item.identifier
            for item in group.interface.items_tree
            if getattr(item, "item_type", "SOCKET") == "SOCKET"
            and item.in_out == "INPUT"
            and item.name == name
        ),
        None,
    )
    if ident is None:
        return None
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    if inputs is not None:
        try:
            return inputs[ident]["value"]
        except (TypeError, KeyError, AttributeError):
            pass
    try:
        return mod[ident]
    except (TypeError, KeyError):
        return None


def _write_modifier_input(mod, ident, value) -> bool:
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    if inputs is not None:
        try:  # Blender 5.x
            inputs[ident]["value"] = value
            return True
        except (TypeError, KeyError, AttributeError):
            pass
    try:  # Blender 4.x
        mod[ident] = value
        return True
    except (TypeError, KeyError):
        return False


# ---------------------------------------------------------------------------
# framing camera
# ---------------------------------------------------------------------------


def scene_bounds(scene: ir.Scene, percentile=2.0):
    """Robust bounds: percentiles, because one stray LiDAR return is normal.

    A min/max box around a cloud with a single return a kilometre out renders
    the whole site as a speck.
    """
    chunks = [c.positions for c in scene.clouds if c.count]
    for line in scene.lines:
        chunks.extend([s for s in line.strips if len(s)])
    for t in scene.transforms:
        if t.translations is not None and len(t.translations):
            chunks.append(t.translations)
    for m in scene.meshes:
        if m.vertices is not None and len(m.vertices):
            chunks.append(m.vertices)
    if not chunks:
        return None
    points = np.concatenate([np.asarray(c, np.float64).reshape(-1, 3) for c in chunks])
    lo = np.percentile(points, percentile, axis=0)
    hi = np.percentile(points, 100.0 - percentile, axis=0)
    return lo, hi


def add_framing_camera(scene: ir.Scene, collection, options, name="Rerun Camera"):
    """A camera that already looks at the data, as a starting point to animate."""
    bounds = scene_bounds(scene)
    cam = bpy.data.cameras.new(name)
    cam.clip_start = 0.05
    cam.clip_end = options.clip_end
    obj = bpy.data.objects.new(name, cam)
    collection.objects.link(obj)

    if bounds is None:
        obj.location = (10.0, -10.0, 10.0)
        obj.rotation_euler = (math.radians(60), 0.0, math.radians(45))
        return obj

    lo, hi = bounds
    centre = (lo + hi) / 2.0
    radius = max(float(np.linalg.norm(hi - lo)) / 2.0, 1.0)
    elevation = math.radians(28.0)
    bearing = math.radians(-135.0)
    distance = radius * 2.4
    eye = centre + np.array([
        math.cos(elevation) * math.cos(bearing),
        math.cos(elevation) * math.sin(bearing),
        math.sin(elevation),
    ]) * distance
    obj.location = Vector(eye)

    direction = Vector(centre - eye)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam.clip_end = max(cam.clip_end, distance * 10.0)

    target = bpy.data.objects.new(f"{name} Target", None)
    target.empty_display_type = "SPHERE"
    target.empty_display_size = radius * 0.05
    target.location = Vector(centre)
    collection.objects.link(target)
    track = obj.constraints.new("TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    return obj
