"""Read an .rrd recording into the :mod:`ir` representation.

The rrd container is Rerun's own versioned format, so this goes through the
Rerun SDK rather than parsing bytes: ``RrdReader`` streams *chunks*, each chunk
converts to a ``pyarrow.RecordBatch``, and every batch carries its timeline
columns (``rerun:kind == "index"``) alongside its component columns
(``rerun:kind == "data"``, named ``Archetype:field``).

That chunk-level path is used on purpose instead of the dataframe/query API:
it needs no query planning, it is what every SDK version since 0.20 exposes in
one shape or another, and a converter wants *all* the rows anyway.

No ``bpy`` here — see :mod:`ir`.
"""

from __future__ import annotations

import numpy as np

from . import ir

# ---------------------------------------------------------------------------
# SDK access
# ---------------------------------------------------------------------------


def _open_store(path: str):
    """Return a chunk-streaming store for ``path``, across SDK generations."""
    import rerun  # noqa: F401  (import error is reported by deps.py)

    try:  # 0.32+
        from rerun.experimental import RrdReader

        return RrdReader(str(path)).store()
    except Exception:
        pass

    from rerun.recording import load_recording  # deprecated in 0.32, gone later

    rec = load_recording(str(path))

    class _Compat:
        def stream(self):
            return iter(rec.chunks())

    return _Compat()


def _chunks(path: str):
    store = _open_store(path)
    return store.stream()


# ---------------------------------------------------------------------------
# arrow helpers
# ---------------------------------------------------------------------------


def _md(field, key: str) -> str:
    meta = field.metadata or {}
    return meta.get(key.encode(), b"").decode()


def _lengths_and_flat(col):
    """Split a ``list<...>`` column into per-row lengths and a flat child array.

    Null rows count as length 0, which is what ``list_flatten`` does too, so
    the two stay in step even when a chunk logs one component sparsely.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
    lengths = pc.list_value_length(col).fill_null(0).to_numpy(zero_copy_only=False)
    return np.asarray(lengths, dtype=np.int64), pc.list_flatten(col)


def _flat_numpy(arr, dtype=None):
    import pyarrow as pa
    import pyarrow.compute as pc

    while pa.types.is_fixed_size_list(arr.type) or pa.types.is_list(arr.type) \
            or pa.types.is_large_list(arr.type):
        arr = pc.list_flatten(arr)
    out = arr.to_numpy(zero_copy_only=False)
    return out if dtype is None else np.asarray(out, dtype=dtype)


def _vecs(col, dim: int):
    """``list<fixed_size_list<float, dim>>`` -> (lengths, (M, dim) float32)."""
    lengths, flat = _lengths_and_flat(col)
    vals = _flat_numpy(flat, np.float32)
    if vals.size % dim:
        vals = vals[: vals.size - (vals.size % dim)]
    return lengths, vals.reshape(-1, dim)


def _scalars(col, dtype):
    lengths, flat = _lengths_and_flat(col)
    return lengths, _flat_numpy(flat, dtype)


def _nested_strips(col):
    """``list<list<fixed_size_list<float,3>>>`` -> list of list of (n, 3)."""
    import pyarrow.compute as pc

    out = []
    for row in col:
        if not row.is_valid:
            out.append([])
            continue
        strips = []
        for strip in row.values:
            if not strip.is_valid:
                strips.append(np.zeros((0, 3), np.float32))
                continue
            xyz = _flat_numpy(pc.list_flatten(strip.values), np.float32)
            strips.append(xyz.reshape(-1, 3) if xyz.size else np.zeros((0, 3), np.float32))
        out.append(strips)
    return out


def _times(col):
    """Index column -> int64 nanoseconds (or raw sequence numbers)."""
    import pyarrow as pa

    if pa.types.is_timestamp(col.type) or pa.types.is_duration(col.type):
        return col.to_numpy(zero_copy_only=False).astype("int64")
    return np.asarray(col.to_numpy(zero_copy_only=False), dtype=np.int64)


def _timeline_kind(arrow_type) -> str:
    import pyarrow as pa

    if pa.types.is_duration(arrow_type):
        return "duration"
    if pa.types.is_timestamp(arrow_type):
        return "timestamp"
    return "sequence"


def _colors_rgba(u32: np.ndarray) -> np.ndarray:
    """Rerun stores Color as one uint32 of 0xRRGGBBAA."""
    v = np.asarray(u32, dtype=np.uint32)
    return np.stack(
        [(v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF], axis=-1
    ).astype(np.uint8)


# ---------------------------------------------------------------------------
# probing (cheap: only what the import dialog needs)
# ---------------------------------------------------------------------------


def probe(path: str) -> ir.Scene:
    """Read only the timeline/entity inventory, for the import dialog."""
    scene = ir.Scene(path=str(path))
    spans: dict = {}
    kinds: dict = {}
    for chunk in _chunks(path):
        batch = chunk.to_record_batch()
        for f in batch.schema:
            if _md(f, "rerun:kind") != "index":
                continue
            t = _times(batch.column(f.name))
            if not len(t):
                continue
            kinds[f.name] = _timeline_kind(f.type)
            lo, hi = int(t.min()), int(t.max())
            if f.name in spans:
                a, b = spans[f.name]
                spans[f.name] = (min(a, lo), max(b, hi))
            else:
                spans[f.name] = (lo, hi)
    for name, (lo, hi) in sorted(spans.items()):
        scene.timelines.append(ir.Timeline(name, kinds[name], lo, hi))
    return scene


def default_timeline(timelines: list) -> "ir.Timeline | None":
    """Pick the timeline a human would have picked.

    ``log_tick`` is a row counter, and ``log_time`` is wall-clock at *logging*
    time, which for a replayed recording is not the time anything happened.
    """
    if not timelines:
        return None
    by_name = {t.name: t for t in timelines}
    for preferred in ("elapsed", "device", "wall", "frame", "frame_nr", "timestamp"):
        if preferred in by_name:
            return by_name[preferred]
    for t in timelines:
        if t.name not in ("log_tick", "log_time"):
            return t
    return timelines[0]


# ---------------------------------------------------------------------------
# full read
# ---------------------------------------------------------------------------

_HANDLED = {
    "Points3D", "Transform3D", "LineStrips3D", "Arrows3D", "Boxes3D", "Mesh3D",
    "Asset3D", "Pinhole", "Scalars", "ViewCoordinates", "RecordingInfo",
    # TransformAxes3D needs nothing built: the empty that carries the
    # transform already displays its axes.
    "TransformAxes3D",
}


class _Acc:
    """Per-entity, per-archetype row accumulator."""

    def __init__(self):
        self.data: dict = {}

    def add(self, entity: str, archetype: str, field: str, times, lengths, values):
        slot = self.data.setdefault((entity, archetype), {})
        slot.setdefault(field, []).append((times, lengths, values))


def read(
    path: str,
    timeline: "str | None" = None,
    max_points: int = 0,
    include: "list | None" = None,
    seed: int = 0,
) -> ir.Scene:
    """Read ``path`` into a :class:`ir.Scene`.

    ``timeline`` names the rrd timeline to animate along (default: the one
    :func:`default_timeline` picks).  ``max_points`` caps each point cloud with
    a deterministic random subsample; 0 means no cap.  ``include`` is a list of
    entity-path prefixes to keep.
    """
    scene = probe(path)
    scene.timeline = (
        next((t for t in scene.timelines if t.name == timeline), None)
        if timeline
        else default_timeline(scene.timelines)
    )
    tl = scene.timeline.name if scene.timeline else None
    t_zero = scene.timeline.t_min if scene.timeline else 0

    acc = _Acc()
    for chunk in _chunks(path):
        entity = chunk.entity_path
        if include and not any(
            entity == p or entity.startswith(p.rstrip("/") + "/") for p in include
        ):
            continue
        batch = chunk.to_record_batch()
        names = set(batch.schema.names)
        times = _times(batch.column(tl)) if tl and tl in names else None

        for f in batch.schema:
            if _md(f, "rerun:kind") != "data":
                continue
            comp = _md(f, "rerun:component") or f.name
            archetype, _, fieldname = comp.partition(":")
            if not fieldname:
                archetype, fieldname = comp, comp
            if archetype not in _HANDLED:
                scene.skipped[archetype] = scene.skipped.get(archetype, 0) + chunk.num_rows
                continue
            col = batch.column(f.name)
            row_times = (
                times
                if times is not None
                else np.full(batch.num_rows, t_zero, dtype=np.int64)
            )
            acc.add(entity, archetype, fieldname, row_times, col, f.type)

    _assemble(scene, acc, t_zero, max_points, seed)
    return scene


def _sorted_rows(entries):
    """Concatenate chunk fragments of one field and sort them by time."""
    import pyarrow as pa

    times = np.concatenate([e[0] for e in entries]) if entries else np.zeros(0, np.int64)
    cols = [e[1] for e in entries]
    col = cols[0] if len(cols) == 1 else pa.concat_arrays(
        [c.combine_chunks() if isinstance(c, pa.ChunkedArray) else c for c in cols]
    )
    order = np.argsort(times, kind="stable")
    return times[order], col.take(pa.array(order))


def _assemble(scene: ir.Scene, acc: _Acc, t_zero: int, max_points: int, seed: int):
    import pyarrow as pa  # noqa: F401  (used via _sorted_rows)

    rng = np.random.default_rng(seed)

    for (entity, archetype), fields in sorted(acc.data.items()):
        if archetype == "Points3D":
            scene.clouds.append(_build_cloud(entity, fields, max_points, rng))
        elif archetype == "Transform3D":
            scene.transforms.append(_build_transform(entity, fields))
        elif archetype == "LineStrips3D":
            scene.lines.append(_build_lines(entity, fields))
        elif archetype == "Arrows3D":
            arrows = _build_arrows(entity, fields)
            if arrows is not None:
                scene.lines.append(arrows)
        elif archetype == "Boxes3D":
            scene.boxes.append(_build_boxes(entity, fields))
        elif archetype == "Mesh3D":
            scene.meshes.append(_build_mesh(entity, fields))
        elif archetype == "Asset3D":
            asset = _build_asset(entity, fields)
            if asset is not None:
                scene.assets.append(asset)
        elif archetype == "Pinhole":
            scene.pinholes.append(_build_pinhole(entity, fields))
        elif archetype == "Scalars":
            scene.scalars.append(_build_scalars(entity, fields))
        elif archetype == "ViewCoordinates":
            scene.view_coordinates = _build_view_coordinates(fields)


def _build_cloud(entity, fields, max_points, rng) -> ir.PointCloud:
    times, col = _sorted_rows(fields["positions"])
    lengths, xyz = _vecs(col, 3)
    birth = np.repeat(times, lengths)

    colors = None
    if "colors" in fields:
        _, ccol = _sorted_rows(fields["colors"])
        clen, cvals = _scalars(ccol, np.uint32)
        colors = _broadcast_per_point(_colors_rgba(cvals), clen, lengths, 4, np.uint8)
    radii = None
    if "radii" in fields:
        _, rcol = _sorted_rows(fields["radii"])
        rlen, rvals = _scalars(rcol, np.float32)
        radii = _broadcast_per_point(rvals.reshape(-1, 1), rlen, lengths, 1, np.float32)
        radii = radii.reshape(-1)

    if max_points and len(xyz) > max_points:
        keep = np.sort(rng.choice(len(xyz), size=max_points, replace=False))
        xyz, birth = xyz[keep], birth[keep]
        colors = None if colors is None else colors[keep]
        radii = None if radii is None else radii[keep]

    return ir.PointCloud(
        path=entity, positions=xyz, colors=colors, radii=radii,
        birth=birth, rows=len(times),
    )


def _broadcast_per_point(values, value_lengths, point_lengths, width, dtype):
    """Match a per-row component to per-point positions.

    Rerun lets a row carry one colour for many points (``clamped`` semantics),
    so a row of 3000 points with a single colour must fan that colour out.
    """
    out = np.zeros((int(point_lengths.sum()), width), dtype=dtype)
    vo = po = 0
    for vlen, plen in zip(value_lengths, point_lengths):
        vlen, plen = int(vlen), int(plen)
        if plen == 0:
            vo += vlen
            continue
        if vlen == 0:
            po += plen
            continue
        chunk = values[vo : vo + vlen]
        if vlen < plen:  # clamp the last value, as Rerun does
            chunk = np.concatenate([chunk, np.repeat(chunk[-1:], plen - vlen, axis=0)])
        out[po : po + plen] = chunk[:plen]
        vo += vlen
        po += plen
    return out


def _build_transform(entity, fields) -> ir.TransformSeries:
    times = None
    translations = matrices = quats = scales = None
    if "translation" in fields:
        times, col = _sorted_rows(fields["translation"])
        _, translations = _vecs(col, 3)
    if "mat3x3" in fields:
        t2, col = _sorted_rows(fields["mat3x3"])
        times = t2 if times is None else times
        _, m = _vecs(col, 9)
        matrices = m.reshape(-1, 3, 3)
    if "quaternion" in fields:
        t2, col = _sorted_rows(fields["quaternion"])
        times = t2 if times is None else times
        _, quats = _vecs(col, 4)
    if "scale" in fields:
        t2, col = _sorted_rows(fields["scale"])
        times = t2 if times is None else times
        _, scales = _vecs(col, 3)
    if times is None:
        times = np.zeros(0, np.int64)
    return ir.TransformSeries(
        path=entity, times=times, translations=translations, matrices=matrices,
        quaternions=quats, scales=scales, static=len(np.unique(times)) <= 1,
    )


def _build_lines(entity, fields) -> ir.LineStrips:
    times, col = _sorted_rows(fields["strips"])
    rows = _nested_strips(col)

    # Final state, plus the first time each vertex index existed. A trajectory
    # re-logged every frame collapses into one growing polyline this way.
    final = rows[-1] if rows else []
    birth = [np.full(len(s), times[-1] if len(times) else 0, np.int64) for s in final]
    for t, strips in zip(times, rows):
        for i, strip in enumerate(strips):
            if i >= len(birth):
                continue
            n = min(len(strip), len(birth[i]))
            np.minimum(birth[i][:n], t, out=birth[i][:n])

    colors: list = []
    if "colors" in fields:
        _, ccol = _sorted_rows(fields["colors"])
        clen, cvals = _scalars(ccol, np.uint32)
        rgba = _colors_rgba(cvals)
        if len(rgba):
            tail = int(clen[-1]) if len(clen) else 0
            last = rgba[len(rgba) - tail :] if tail else rgba[-1:]
            colors = [last[min(i, len(last) - 1)] for i in range(len(final))]
    radii: list = []
    if "radii" in fields:
        _, rcol = _sorted_rows(fields["radii"])
        rlen, rvals = _scalars(rcol, np.float32)
        if len(rvals):
            tail = int(rlen[-1]) if len(rlen) else 0
            last = rvals[len(rvals) - tail :] if tail else rvals[-1:]
            radii = [float(last[min(i, len(last) - 1)]) for i in range(len(final))]

    return ir.LineStrips(
        path=entity, strips=final, birth=birth, colors=colors, radii=radii,
        rows=len(rows),
    )


def _build_arrows(entity, fields):
    """Arrows3D -> two-point strips, so they ride the line-strip pipeline.

    Only the last row is kept: arrows are re-logged in full each row (a vector
    field, a set of axes), so accumulating them would pile every past state on
    top of the present one.
    """
    if "vectors" not in fields:
        return None
    times, col = _sorted_rows(fields["vectors"])
    lengths, vectors = _vecs(col, 3)
    if not len(lengths):
        return None

    last = int(lengths[-1])
    if not last:
        return None
    vectors = vectors[len(vectors) - last :]
    time = int(times[-1]) if len(times) else 0

    origins = np.zeros_like(vectors)
    if "origins" in fields:
        _, ocol = _sorted_rows(fields["origins"])
        olen, ovals = _vecs(ocol, 3)
        origins = _broadcast_per_point(ovals, olen, np.array([last]), 3, np.float32)

    colors: list = []
    if "colors" in fields:
        _, ccol = _sorted_rows(fields["colors"])
        clen, cvals = _scalars(ccol, np.uint32)
        rgba = _colors_rgba(cvals)
        if len(rgba):
            fanned = _broadcast_per_point(rgba, clen[-1:], np.array([last]), 4, np.uint8)
            colors = [fanned[i] for i in range(last)]
    radii: list = []
    if "radii" in fields:
        _, rcol = _sorted_rows(fields["radii"])
        rlen, rvals = _scalars(rcol, np.float32)
        if len(rvals):
            fanned = _broadcast_per_point(
                rvals.reshape(-1, 1), rlen[-1:], np.array([last]), 1, np.float32
            )
            radii = [float(fanned[i][0]) for i in range(last)]

    strips = [
        np.stack([origins[i], origins[i] + vectors[i]]).astype(np.float32)
        for i in range(last)
    ]
    return ir.LineStrips(
        path=entity,
        strips=strips,
        birth=[np.full(2, time, np.int64) for _ in range(last)],
        colors=colors,
        radii=radii,
        rows=len(lengths),
    )


def _build_boxes(entity, fields) -> ir.Boxes:
    key = "half_sizes" if "half_sizes" in fields else next(iter(fields))
    times, col = _sorted_rows(fields[key])
    lengths, half = _vecs(col, 3)
    birth = np.repeat(times, lengths)
    centers = np.zeros_like(half)
    if "centers" in fields:
        _, ccol = _sorted_rows(fields["centers"])
        clen, cvals = _vecs(ccol, 3)
        centers = _broadcast_per_point(cvals, clen, lengths, 3, np.float32)
    colors = None
    if "colors" in fields:
        _, ccol = _sorted_rows(fields["colors"])
        clen, cvals = _scalars(ccol, np.uint32)
        colors = _broadcast_per_point(_colors_rgba(cvals), clen, lengths, 4, np.uint8)
    quats = None
    if "quaternions" in fields:
        _, qcol = _sorted_rows(fields["quaternions"])
        qlen, qvals = _vecs(qcol, 4)
        quats = _broadcast_per_point(qvals, qlen, lengths, 4, np.float32)
    return ir.Boxes(
        path=entity, centers=centers, half_sizes=half, colors=colors,
        quaternions=quats, birth=birth, rows=len(times),
    )


def _build_mesh(entity, fields) -> ir.TriMesh:
    mesh = ir.TriMesh(path=entity)
    if "vertex_positions" in fields:
        _, col = _sorted_rows(fields["vertex_positions"])
        lengths, xyz = _vecs(col, 3)
        mesh.vertices = xyz[-int(lengths[-1]) :] if len(lengths) and lengths[-1] else xyz
    if "triangle_indices" in fields:
        _, col = _sorted_rows(fields["triangle_indices"])
        lengths, idx = _vecs(col, 3)
        idx = idx.astype(np.int64)
        mesh.triangles = idx[-int(lengths[-1]) :] if len(lengths) and lengths[-1] else idx
    if "vertex_colors" in fields:
        _, col = _sorted_rows(fields["vertex_colors"])
        lengths, vals = _scalars(col, np.uint32)
        rgba = _colors_rgba(vals)
        mesh.colors = rgba[-int(lengths[-1]) :] if len(lengths) and lengths[-1] else rgba
    if "vertex_normals" in fields:
        _, col = _sorted_rows(fields["vertex_normals"])
        lengths, vals = _vecs(col, 3)
        mesh.normals = vals[-int(lengths[-1]) :] if len(lengths) and lengths[-1] else vals
    if "vertex_texcoords" in fields:
        _, col = _sorted_rows(fields["vertex_texcoords"])
        lengths, vals = _vecs(col, 2)
        mesh.uvs = vals[-int(lengths[-1]) :] if len(lengths) and lengths[-1] else vals
    return mesh


def _build_asset(entity, fields):
    if "blob" not in fields:
        return None
    _, col = _sorted_rows(fields["blob"])
    row = col[len(col) - 1]
    if not row.is_valid:
        return None
    blob = bytes(np.asarray(_flat_numpy(row.values, np.uint8)))
    media = ""
    if "media_type" in fields:
        _, mcol = _sorted_rows(fields["media_type"])
        try:
            vals = mcol[len(mcol) - 1].as_py()
            media = (vals[-1] if isinstance(vals, list) else vals) or ""
        except Exception:
            media = ""
    return ir.Asset(path=entity, blob=blob, media_type=media)


def _build_pinhole(entity, fields) -> ir.Pinhole:
    ph = ir.Pinhole(path=entity)
    if "image_from_camera" in fields:
        _, col = _sorted_rows(fields["image_from_camera"])
        _, m = _vecs(col, 9)
        if len(m):
            k = m[-1].reshape(3, 3)
            # Rerun stores this matrix column-major.
            ph.focal = np.array([k[0, 0], k[1, 1]], np.float32)
            ph.principal = np.array([k[2, 0], k[2, 1]], np.float32)
    if "resolution" in fields:
        _, col = _sorted_rows(fields["resolution"])
        _, res = _vecs(col, 2)
        if len(res):
            ph.resolution = res[-1]
    return ph


def _build_scalars(entity, fields) -> ir.Scalars:
    times, col = _sorted_rows(fields["scalars"])
    lengths, vals = _scalars(col, np.float64)
    keep = lengths > 0
    first = np.zeros(len(lengths), np.float64)
    off = 0
    for i, n in enumerate(lengths):
        if n:
            first[i] = vals[off]
        off += int(n)
    return ir.Scalars(path=entity, times=times[keep], values=first[keep])


def _build_view_coordinates(fields):
    for key in ("xyz", "coordinates"):
        if key in fields:
            _, col = _sorted_rows(fields[key])
            vals = _flat_numpy(col, np.uint8)
            if len(vals) >= 3:
                axes = vals[-3:]
                return "".join(ir.VIEW_DIR.get(int(a), "?") for a in axes)
    return None
