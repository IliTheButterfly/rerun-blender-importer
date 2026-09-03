# Rerun (.rrd) importer for Blender

Turn a [Rerun](https://rerun.io) recording into an ordinary Blender scene —
animated point clouds, posed entities, growing trajectories, boxes, meshes and
cameras — so you can light it, composite it, and fly a Blender camera through
it.

There is no official Rerun → Blender path, and no exchange format that carries
what an .rrd carries (a point cloud whose size changes every row, colours,
per-entity transforms over time). So this add-on reads the recording with
Rerun's own SDK and rebuilds it as Blender data.

## What you get

| In the recording | In Blender |
| --- | --- |
| `Points3D` | one mesh holding every point, drawn as a point cloud by geometry nodes, revealed over time |
| `Transform3D` | an empty per entity path, keyframed (linear), with entities parented under it |
| `LineStrips3D` | a polyline meshed into tubes, drawing itself as the recording drew it |
| `Arrows3D` | one tube per arrow, from its origin to its tip (last row only) |
| `Boxes3D` | box instances with per-box size, rotation and colour |
| `Mesh3D` | a real mesh, with vertex colours and UVs |
| `Asset3D` | the embedded glb/gltf/obj/stl/ply, unpacked and imported |
| `Pinhole` | a Blender camera with the recording's focal length and principal point |
| `Scalars` | optional: keyframed custom properties you can drive things with |
| `ViewCoordinates` | read and reported (Rerun's `RFU` is already Blender's convention) |
| colours | an sRGB → linear colour attribute plus a material that shows it |
| timelines | your pick of timeline, mapped onto Blender frames at your fps |

## Install

Download `rerun_importer-<version>.zip` from the
[releases](https://github.com/IliTheButterfly/rerun-blender-importer/releases),
then in Blender: **Edit → Preferences → Add-ons → ⌄ → Install from Disk**.

Reading .rrd needs Rerun's own reader, which Blender does not ship. The add-on
does **not** silently download anything: open the **Rerun** tab in the 3D
viewport sidebar (`N`) and press **Install Rerun SDK** once. It fetches
`rerun-sdk` and `pyarrow` (~200 MB) into the add-on's own directory — nothing
outside it, and nothing in your system Python. It works even where Blender's
Python has no `pip` (it runs pip out of the wheel `ensurepip` carries), and it
prefers [`uv`](https://docs.astral.sh/uv/) if you have it.

No network on the machine that runs Blender? Install the wheels by hand into
the add-on's own directory instead — the add-on prints the exact command (and
the path) when it cannot install them itself.

Requires Blender 4.2 or newer.

## Use it

**File → Import → Rerun Recording (.rrd)**. The dialog reads the file and lists
its timelines; the defaults are chosen to be the boring right answer.

The options that matter:

- **Timeline** — which of the recording's timelines becomes Blender's. `log_tick`
  and `log_time` are deliberately not preferred: one is a row counter, the other
  is when the logging happened, which for a replayed recording is not when
  anything happened.
- **FPS** and **Time Scale** — how recorded time maps to frames. A 14-minute
  flight at 30 fps is 25 000 frames; a Time Scale of 20 makes it 1 250.
- **Trail** — how many frames a point stays visible. `0` accumulates forever,
  which is what a SLAM map wants. A small value (say `2`) gives a per-frame
  sensor sweep instead. Changeable per object afterwards, on the modifier or via
  **Set Trail** in the sidebar.
- **Point Budget** — a per-cloud cap, randomly subsampled. Raise or remove it
  for a final render; lower it while you block out a shot.
- **Emissive Colours** — on, the logged colours glow like they do in the Rerun
  viewer. Off, they become lit surfaces you can shade properly.

Then animate a camera as usual. Two shortcuts in the **Rerun** sidebar tab:
the import already adds a camera framed on the data, and **Add Chase Camera**
attaches one to the selected entity — pick the drone empty for a follow shot.

### Headless

```bash
blender -b -P tools/rrd2blend.py -- flight.rrd -o flight.blend --fps 30
blender -b -P tools/rrd2blend.py -- flight.rrd --list          # what is in it
blender -b -P tools/rrd2blend.py -- flight.rrd --render f.png --trail 2 \
    --include /world/map --max-points 500000
```

## How it works, and why that way

**Time is a point attribute, not a mesh per frame.** Rerun animates by
appending: a map is 150 rows of "here are 2000 more points". Blender cannot
keyframe a changing vertex count, so every point is baked once, tagged with the
frame it arrived on (`rr_birth`), and a Delete Geometry driven by the Scene Time
node hides the future. Scrubbing replays the recording with no per-frame file
IO, and one modifier input turns an accumulating map into a sensor sweep.

**Entity paths become a parent hierarchy.** Rerun resolves a transform at the
cursor time and applies it to everything below it — which is exactly what
Blender parenting does. So a cloud logged in a sensor frame moves with the
drone, and a cloud logged in world coordinates stays put. Pre-multiplying
instead is how you get the classic "the map slides along with the drone" bug.

**A re-logged trajectory collapses into one growing polyline.** 3000 rows of
"the path so far" become one mesh whose vertices carry their own birth frame,
rather than 3000 copies of nearly the same curve.

**Colours are converted sRGB → linear.** Rerun logs 8-bit sRGB; Blender shades
in linear. Skip that and everything is washed out.

The reader (`rrd_reader.py`) goes through Rerun's chunk stream and pyarrow, and
is free of `bpy`; the builder (`scene_builder.py`, `nodegroups.py`) is free of
Rerun. The intermediate representation between them is `ir.py`.

## Not imported

- `Image`, `DepthImage`, `SegmentationImage`, `EncodedImage`, video — camera
  imagery is not projected into the scene (the `Pinhole` camera itself is).
- `Points2D`, `LineStrips2D`, `Boxes2D` and other 2D-space archetypes.
- Annotation contexts (class-id colouring), text logs, `Tensor`.
- `Arrows3D` heads: an arrow imports as a shaft, without a cone on the end.
- Per-row `Mesh3D` deformation: a mesh logged repeatedly imports its last row.

Anything unsupported is counted and reported after the import rather than
silently dropped — `--list` shows it up front.

## Tests

```bash
make test           # reader + build tests, then a throwaway-profile install test
make test-reader    # the bpy-free read path
make test-blender   # build, animation and "the render is not black" checks
make test-install   # install the zip into a temp profile, fetch the SDK, import
```

`test-install` runs Blender twice against the same throwaway profile — install,
then reopen — so it also proves the add-on and its SDK survive a restart. It
refuses to run unless `BLENDER_USER_RESOURCES` reached Blender, so it can never
touch your real configuration; if Blender lives behind a hop, name it once with
`make test HOST=distrobox-host-exec` (or point `BLENDER` at it).

The suites generate their own recording with whatever Rerun SDK is installed,
rather than committing a fixture that would freeze one format version. Every
gate has been mutation-tested: break the sRGB conversion, the colour clamping,
the material socket or the birth times, and a test fails.

## Licence

MIT — see [LICENSE](LICENSE).
