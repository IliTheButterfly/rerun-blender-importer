"""Intermediate representation of an .rrd recording.

Deliberately free of ``bpy`` so it can be exercised by plain ``pytest`` outside
Blender.  The reader (:mod:`rrd_reader`) fills these in, the builder
(:mod:`scene_builder`) turns them into Blender data — neither knows about the
other's world.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

try:  # numpy ships with Blender, but keep the import honest for tooling
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore


# Rerun's ViewDir enum, as stored in the ViewCoordinates component.
VIEW_DIR = {1: "U", 2: "D", 3: "R", 4: "L", 5: "F", 6: "B"}


@dataclass
class Timeline:
    """One rrd timeline and the span of values actually present in the file."""

    name: str
    kind: str  # "duration" | "timestamp" | "sequence"
    t_min: int = 0
    t_max: int = 0

    @property
    def is_temporal(self) -> bool:
        return self.kind in ("duration", "timestamp")

    def label(self) -> str:
        if self.is_temporal:
            span = (self.t_max - self.t_min) / 1e9
            return f"{self.name} ({span:.1f} s)"
        return f"{self.name} ({self.t_max - self.t_min + 1} steps)"


@dataclass
class Entity:
    """Base for everything that lands in the scene as one Blender object."""

    path: str

    @property
    def name(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1] or "root"


@dataclass
class PointCloud(Entity):
    """All Points3D rows of one entity, concatenated.

    Every point carries the timeline value of the row it arrived on
    (``birth``), which is what lets the builder animate an accumulating map
    without storing a mesh per frame.
    """

    positions: Any = None  # (N, 3) float32
    colors: Any = None     # (N, 4) uint8
    radii: Any = None      # (N,)   float32
    birth: Any = None      # (N,)   int64, timeline value
    rows: int = 0

    @property
    def count(self) -> int:
        return 0 if self.positions is None else len(self.positions)


@dataclass
class TransformSeries(Entity):
    """Transform3D over time: one keyframe per row."""

    times: Any = None          # (T,) int64
    translations: Any = None   # (T, 3) float32
    matrices: Any = None       # (T, 3, 3) float32 or None
    quaternions: Any = None    # (T, 4) xyzw or None
    scales: Any = None         # (T, 3) float32 or None
    static: bool = False


@dataclass
class LineStrips(Entity):
    """LineStrips3D rows.

    ``strips`` holds the strips of the *last* row (the final state), and
    ``birth`` gives, per point, the first timeline value at which that point
    existed — so a trajectory that is re-logged as it grows animates as a
    growing trail instead of 1500 redundant copies.
    """

    strips: list = field(default_factory=list)   # list[(n, 3) float32]
    birth: list = field(default_factory=list)    # list[(n,) int64]
    colors: list = field(default_factory=list)   # list[(4,) uint8]
    radii: list = field(default_factory=list)    # list[float]
    rows: int = 0


@dataclass
class Boxes(Entity):
    """Boxes3D, flattened the same way as point clouds."""

    centers: Any = None       # (N, 3)
    half_sizes: Any = None    # (N, 3)
    colors: Any = None        # (N, 4) uint8
    quaternions: Any = None   # (N, 4) xyzw or None
    birth: Any = None         # (N,)
    rows: int = 0


@dataclass
class TriMesh(Entity):
    """Mesh3D — vertices plus optional triangle indices and vertex colours."""

    vertices: Any = None
    triangles: Any = None
    colors: Any = None
    normals: Any = None
    uvs: Any = None


@dataclass
class Asset(Entity):
    """Asset3D — an embedded model file (glb/gltf/obj/stl/ply)."""

    blob: bytes = b""
    media_type: str = ""


@dataclass
class Pinhole(Entity):
    """Pinhole camera intrinsics."""

    focal: Any = None        # (2,) pixels
    resolution: Any = None   # (2,) pixels
    principal: Any = None    # (2,) pixels or None


@dataclass
class Scalars(Entity):
    """Scalars — kept so plots can drive things in Blender."""

    times: Any = None
    values: Any = None


@dataclass
class Scene:
    """Everything the builder needs, and nothing about Blender."""

    path: str = ""
    application_id: str = ""
    recording_id: str = ""
    timelines: list = field(default_factory=list)          # list[Timeline]
    timeline: Optional[Timeline] = None                    # the one that was read
    view_coordinates: Optional[str] = None                 # e.g. "RFU"
    clouds: list = field(default_factory=list)
    transforms: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    boxes: list = field(default_factory=list)
    meshes: list = field(default_factory=list)
    assets: list = field(default_factory=list)
    pinholes: list = field(default_factory=list)
    scalars: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)            # archetype -> count

    def all_entities(self) -> list:
        return [
            *self.clouds, *self.transforms, *self.lines, *self.boxes,
            *self.meshes, *self.assets, *self.pinholes, *self.scalars,
        ]

    def summary(self) -> str:
        pts = sum(c.count for c in self.clouds)
        bits = []
        if self.clouds:
            bits.append(f"{len(self.clouds)} point cloud(s), {pts:,} points")
        if self.transforms:
            bits.append(f"{len(self.transforms)} transform(s)")
        if self.lines:
            bits.append(f"{len(self.lines)} line strip entity(ies)")
        if self.boxes:
            bits.append(f"{len(self.boxes)} box entity(ies)")
        if self.meshes or self.assets:
            bits.append(f"{len(self.meshes) + len(self.assets)} mesh/asset(s)")
        if self.pinholes:
            bits.append(f"{len(self.pinholes)} camera(s)")
        if self.scalars:
            bits.append(f"{len(self.scalars)} scalar series")
        return ", ".join(bits) or "nothing importable"
