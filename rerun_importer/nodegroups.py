"""Geometry node groups that give imported data its behaviour over time.

Rerun animates by *appending* data: a SLAM map is 150 rows of "here are 2000
more points", a trajectory is 3000 rows of "here is the path so far".  Blender
cannot keyframe a changing vertex count, so instead of one mesh per frame this
bakes every point once, tags it with the Blender frame it arrived on
(``rr_birth``), and hides the future with a Delete Geometry driven by the Scene
Time node.  Scrubbing the timeline then replays the recording with no per-frame
file IO, and one modifier input turns an accumulating map into a per-frame
sensor sweep.
"""

from __future__ import annotations

import bpy

BIRTH_ATTR = "rr_birth"
RADIUS_ATTR = "rr_radius"
COLOR_ATTR = "Col"
HALF_SIZE_ATTR = "rr_half_size"
ROTATION_ATTR = "rr_rotation"

CLOUD_GROUP = "Rerun Point Cloud"
LINES_GROUP = "Rerun Line Strips"
BOXES_GROUP = "Rerun Boxes"


def _socket(group, name, socket_type, in_out="INPUT", **kwargs):
    sock = group.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    for key, value in kwargs.items():
        try:
            setattr(sock, key, value)
        except (AttributeError, TypeError):
            pass
    return sock


def _time_selection(group, nodes, links, x=-400):
    """Build "this point does not exist yet (or has expired)" as a selection.

    Returns the boolean socket to feed Delete Geometry, i.e. True for the
    points that must go.  ``Trail`` of 0 means "keep forever" (an accumulating
    map); a small trail (0.5) shows only the current row, which is what a
    non-accumulating sensor cloud wants.
    """
    birth = nodes.new("GeometryNodeInputNamedAttribute")
    birth.data_type = "FLOAT"
    birth.inputs["Name"].default_value = BIRTH_ATTR
    birth.location = (x, 200)

    time = nodes.new("GeometryNodeInputSceneTime")
    time.location = (x, 40)

    unborn = nodes.new("FunctionNodeCompare")
    unborn.data_type = "FLOAT"
    unborn.operation = "GREATER_THAN"
    unborn.location = (x + 200, 160)
    links.new(birth.outputs["Attribute"], unborn.inputs[0])
    links.new(time.outputs["Frame"], unborn.inputs[1])

    age = nodes.new("ShaderNodeMath")
    age.operation = "SUBTRACT"
    age.location = (x + 200, 0)
    links.new(time.outputs["Frame"], age.inputs[0])
    links.new(birth.outputs["Attribute"], age.inputs[1])

    group_in = next(n for n in nodes if n.bl_idname == "NodeGroupInput")

    expired = nodes.new("FunctionNodeCompare")
    expired.data_type = "FLOAT"
    expired.operation = "GREATER_THAN"
    expired.location = (x + 400, -40)
    links.new(age.outputs[0], expired.inputs[0])
    links.new(group_in.outputs["Trail"], expired.inputs[1])

    has_trail = nodes.new("FunctionNodeCompare")
    has_trail.data_type = "FLOAT"
    has_trail.operation = "GREATER_THAN"
    has_trail.inputs[1].default_value = 0.0
    has_trail.location = (x + 400, -200)
    links.new(group_in.outputs["Trail"], has_trail.inputs[0])

    trailed = nodes.new("FunctionNodeBooleanMath")
    trailed.operation = "AND"
    trailed.location = (x + 600, -120)
    links.new(expired.outputs[0], trailed.inputs[0])
    links.new(has_trail.outputs[0], trailed.inputs[1])

    gone = nodes.new("FunctionNodeBooleanMath")
    gone.operation = "OR"
    gone.location = (x + 800, 40)
    links.new(unborn.outputs[0], gone.inputs[0])
    links.new(trailed.outputs[0], gone.inputs[1])
    return gone.outputs[0]


def _filtered_geometry(group, nodes, links, geometry_socket):
    """Geometry with future/expired points deleted, switchable via ``Animate``."""
    group_in = next(n for n in nodes if n.bl_idname == "NodeGroupInput")

    delete = nodes.new("GeometryNodeDeleteGeometry")
    delete.domain = "POINT"
    delete.mode = "ALL"
    delete.location = (700, 200)
    links.new(geometry_socket, delete.inputs["Geometry"])
    links.new(_time_selection(group, nodes, links), delete.inputs["Selection"])

    switch = nodes.new("GeometryNodeSwitch")
    switch.input_type = "GEOMETRY"
    switch.location = (900, 200)
    links.new(group_in.outputs["Animate"], switch.inputs["Switch"])
    links.new(geometry_socket, switch.inputs[False])
    links.new(delete.outputs["Geometry"], switch.inputs[True])
    return switch.outputs[0]


def _new_group(name):
    if name in bpy.data.node_groups:
        return bpy.data.node_groups[name], False
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    _socket(group, "Geometry", "NodeSocketGeometry")
    _socket(group, "Geometry", "NodeSocketGeometry", in_out="OUTPUT")
    _socket(group, "Animate", "NodeSocketBool", default_value=True)
    _socket(group, "Trail", "NodeSocketFloat", default_value=0.0, min_value=0.0)
    _socket(group, "Material", "NodeSocketMaterial")
    return group, True


def point_cloud_group() -> bpy.types.NodeTree:
    """Points -> time-filtered point cloud with per-point radius and colour."""
    group, fresh = _new_group(CLOUD_GROUP)
    if not fresh:
        return group
    _socket(group, "Radius Scale", "NodeSocketFloat", default_value=1.0, min_value=0.0)
    _socket(group, "Min Radius", "NodeSocketFloat", default_value=0.01, min_value=0.0)

    nodes, links = group.nodes, group.links
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-800, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1600, 200)

    filtered = _filtered_geometry(group, nodes, links, group_in.outputs["Geometry"])

    radius_attr = nodes.new("GeometryNodeInputNamedAttribute")
    radius_attr.data_type = "FLOAT"
    radius_attr.inputs["Name"].default_value = RADIUS_ATTR
    radius_attr.location = (900, -200)

    scaled = nodes.new("ShaderNodeMath")
    scaled.operation = "MULTIPLY"
    scaled.location = (1080, -200)
    links.new(radius_attr.outputs["Attribute"], scaled.inputs[0])
    links.new(group_in.outputs["Radius Scale"], scaled.inputs[1])

    floored = nodes.new("ShaderNodeMath")
    floored.operation = "MAXIMUM"
    floored.location = (1240, -200)
    links.new(scaled.outputs[0], floored.inputs[0])
    links.new(group_in.outputs["Min Radius"], floored.inputs[1])

    to_points = nodes.new("GeometryNodeMeshToPoints")
    to_points.mode = "VERTICES"
    to_points.location = (1240, 200)
    links.new(filtered, to_points.inputs["Mesh"])
    links.new(floored.outputs[0], to_points.inputs["Radius"])

    set_mat = nodes.new("GeometryNodeSetMaterial")
    set_mat.location = (1420, 200)
    links.new(to_points.outputs["Points"], set_mat.inputs["Geometry"])
    links.new(group_in.outputs["Material"], set_mat.inputs["Material"])
    links.new(set_mat.outputs["Geometry"], group_out.inputs["Geometry"])
    return group


def line_strips_group() -> bpy.types.NodeTree:
    """Polylines -> time-filtered tubes, so a trajectory draws itself."""
    group, fresh = _new_group(LINES_GROUP)
    if not fresh:
        return group
    _socket(group, "Radius", "NodeSocketFloat", default_value=0.05, min_value=0.0)
    _socket(group, "Profile Resolution", "NodeSocketInt", default_value=6, min_value=3)

    nodes, links = group.nodes, group.links
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-800, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1800, 200)

    filtered = _filtered_geometry(group, nodes, links, group_in.outputs["Geometry"])

    to_curve = nodes.new("GeometryNodeMeshToCurve")
    to_curve.location = (1100, 200)
    links.new(filtered, to_curve.inputs["Mesh"])

    circle = nodes.new("GeometryNodeCurvePrimitiveCircle")
    circle.mode = "RADIUS"
    circle.location = (1100, -160)
    links.new(group_in.outputs["Radius"], circle.inputs["Radius"])
    links.new(group_in.outputs["Profile Resolution"], circle.inputs["Resolution"])

    to_mesh = nodes.new("GeometryNodeCurveToMesh")
    to_mesh.location = (1400, 200)
    to_mesh.inputs["Fill Caps"].default_value = True
    links.new(to_curve.outputs["Curve"], to_mesh.inputs["Curve"])
    links.new(circle.outputs["Curve"], to_mesh.inputs["Profile Curve"])

    set_mat = nodes.new("GeometryNodeSetMaterial")
    set_mat.location = (1620, 200)
    links.new(to_mesh.outputs["Mesh"], set_mat.inputs["Geometry"])
    links.new(group_in.outputs["Material"], set_mat.inputs["Material"])
    links.new(set_mat.outputs["Geometry"], group_out.inputs["Geometry"])
    return group


def boxes_group() -> bpy.types.NodeTree:
    """Points carrying half-sizes -> time-filtered box instances."""
    group, fresh = _new_group(BOXES_GROUP)
    if not fresh:
        return group
    _socket(group, "Wireframe Radius", "NodeSocketFloat", default_value=0.0, min_value=0.0)

    nodes, links = group.nodes, group.links
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-800, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1600, 200)

    filtered = _filtered_geometry(group, nodes, links, group_in.outputs["Geometry"])

    cube = nodes.new("GeometryNodeMeshCube")
    cube.location = (900, -260)
    cube.inputs["Size"].default_value = (1.0, 1.0, 1.0)

    half = nodes.new("GeometryNodeInputNamedAttribute")
    half.data_type = "FLOAT_VECTOR"
    half.inputs["Name"].default_value = HALF_SIZE_ATTR
    half.location = (900, -60)

    size = nodes.new("ShaderNodeVectorMath")
    size.operation = "SCALE"
    size.location = (1080, -60)
    links.new(half.outputs["Attribute"], size.inputs[0])
    size.inputs["Scale"].default_value = 2.0

    rotation = nodes.new("GeometryNodeInputNamedAttribute")
    rotation.data_type = "QUATERNION"
    rotation.inputs["Name"].default_value = ROTATION_ATTR
    rotation.location = (900, 60)

    instance = nodes.new("GeometryNodeInstanceOnPoints")
    instance.location = (1260, 200)
    links.new(filtered, instance.inputs["Points"])
    links.new(cube.outputs["Mesh"], instance.inputs["Instance"])
    links.new(size.outputs["Vector"], instance.inputs["Scale"])
    try:
        links.new(rotation.outputs["Attribute"], instance.inputs["Rotation"])
    except Exception:
        pass

    set_mat = nodes.new("GeometryNodeSetMaterial")
    set_mat.location = (1440, 200)
    links.new(instance.outputs["Instances"], set_mat.inputs["Geometry"])
    links.new(group_in.outputs["Material"], set_mat.inputs["Material"])
    links.new(set_mat.outputs["Geometry"], group_out.inputs["Geometry"])
    return group


def rerun_material(name="Rerun Vertex Colour", emissive=True, strength=1.0):
    """A material that shows the colours the recording actually logged."""
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    attr = nodes.new("ShaderNodeAttribute")
    attr.attribute_name = COLOR_ATTR
    attr.location = (-320, 0)
    if bsdf is not None:
        links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
        # Point clouds in the Rerun viewer are unshaded; emission is what makes
        # a Blender render read like the thing the user was looking at.
        for socket, value in (("Emission Color", None), ("Emission Strength", strength if emissive else 0.0)):
            if socket in bsdf.inputs:
                if value is None:
                    links.new(attr.outputs["Color"], bsdf.inputs[socket])
                else:
                    bsdf.inputs[socket].default_value = value
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.6
    mat.use_backface_culling = False
    return mat
