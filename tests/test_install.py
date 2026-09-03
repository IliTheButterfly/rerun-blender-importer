"""Install the built zip into a throwaway Blender profile and use it.

    BLENDER_USER_RESOURCES=$(mktemp -d) \
      blender -b --factory-startup -P tests/test_install.py -- dist/rerun_importer.zip

This is the test that answers "does installation work cleanly": it installs
from the zip the way a user would, enables the add-on, checks the menu entry
appeared, then — since a fresh install has no Rerun SDK — runs the add-on's own
dependency installer and imports a real recording with it.

Run it twice against the same profile to prove the install survives a restart,
which is the half that a single run cannot see:

    blender -b --factory-startup -P tests/test_install.py -- <zip> --stage install
    blender -b                   -P tests/test_install.py -- <zip> --stage verify

The verify stage deliberately does *not* put the repository on ``sys.path``, so
it can only pass on what the installed add-on carries.  ``make test-install``
runs both.

``--skip-deps`` stops before the (large) download, for a quick registration
check.
"""

from __future__ import annotations

import os
import sys
import tempfile

FAILURES = []
PASSES = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} — {detail}")


def _newest_zip():
    import glob

    candidates = sorted(glob.glob("dist/rerun_importer-*.zip"))
    return os.path.abspath(candidates[-1]) if candidates else "dist/rerun_importer.zip"


def main(argv):
    import bpy

    positional = [a for a in argv if not a.startswith("-")]
    zip_path = os.path.abspath(positional[0]) if positional else _newest_zip()
    skip_deps = "--skip-deps" in argv
    stage = "install"
    for arg in argv:
        if arg.startswith("--stage"):
            stage = arg.split("=", 1)[1] if "=" in arg else "install"
    if stage == "verify":
        return verify(zip_path)

    print(f"profile: {bpy.utils.resource_path('USER')}")
    print(f"zip:     {zip_path} ({os.path.getsize(zip_path):,} bytes)")
    check("using a throwaway profile", "BLENDER_USER_RESOURCES" in os.environ,
          "set BLENDER_USER_RESOURCES so this cannot touch a real install")
    if FAILURES:
        return 2

    result = bpy.ops.extensions.package_install_files(
        filepath=zip_path, repo="user_default", enable_on_install=True
    )
    check("package_install_files succeeded", result == {"FINISHED"}, result)

    module = "bl_ext.user_default.rerun_importer"
    check("add-on module is loaded", module in sys.modules,
          sorted(n for n in sys.modules if "rerun" in n))
    check("add-on is enabled", module in bpy.context.preferences.addons,
          [a.module for a in bpy.context.preferences.addons])
    check("import operator is registered", hasattr(bpy.ops.import_scene, "rrd"))
    check("sidebar panel is registered", hasattr(bpy.types, "RRD_PT_panel"))
    check("dependency operator is registered", hasattr(bpy.ops.rrd, "install_dependencies"))

    menu_entries = [
        getattr(func, "__module__", "")
        for func in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize()
    ]
    check("File > Import entry added", any(module in entry for entry in menu_entries),
          menu_entries)

    addon = sys.modules.get(module)
    if addon is None:
        return 1
    deps = addon.deps
    print(f"  SDK before install: {deps.versions()}")

    if skip_deps:
        print("  (skipping the dependency install)")
    else:
        check("a clean install starts without the SDK", not deps.available(),
              deps.versions())
        print("  installing the Rerun SDK — this downloads ~200 MB…")
        try:
            deps.install()
            installed, reason = True, ""
        except RuntimeError as exc:
            installed, reason = False, str(exc)[:800]
        check("the add-on can install its own dependencies", installed, reason)
        check("the SDK imports after installing", deps.available(), deps.versions())
        if installed:
            print(f"  SDK after install: {deps.versions()}")

    if deps.available():
        # Generate a recording and import it through the operator, exactly as
        # the menu entry would.
        import rerun as rr

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "install_check.rrd")
            rec = rr.RecordingStream("install_check")
            rec.save(path)
            for step in range(2):
                rec.set_time("t", duration=float(step))
                rec.log("/world/points", rr.Points3D(
                    positions=[[step, 0, 0], [step, 1, 0]],
                    colors=[[200, 100, 50]] * 2,
                ))
            rec.flush()
            del rec

            result = bpy.ops.import_scene.rrd(filepath=path)
            check("importing through the operator works", result == {"FINISHED"}, result)
            check("the recording became objects",
                  any(o.name == "points" for o in bpy.data.objects),
                  [o.name for o in bpy.data.objects])
            cloud = bpy.data.objects.get("points")
            if cloud:
                check("points arrived", len(cloud.data.vertices) == 4,
                      len(cloud.data.vertices))
                check("geometry nodes attached",
                      any(m.type == "NODES" for m in cloud.modifiers))

    # Blender's GUI auto-saves preferences after an install; headless does not,
    # and without this the add-on comes back disabled on the next launch.
    bpy.ops.wm.save_userpref()
    print("  preferences saved")

    return report()


def verify(zip_path):
    """Second launch, same profile: did any of it survive?"""
    import bpy

    module = "bl_ext.user_default.rerun_importer"
    print(f"profile: {bpy.utils.resource_path('USER')}")
    check("using a throwaway profile", "BLENDER_USER_RESOURCES" in os.environ)
    check("the add-on is still enabled after a restart",
          module in {addon.module for addon in bpy.context.preferences.addons},
          [a.module for a in bpy.context.preferences.addons])
    check("the add-on module loaded itself", module in sys.modules,
          sorted(n for n in sys.modules if "rerun_importer" in n))
    check("import operator is registered", hasattr(bpy.ops.import_scene, "rrd"))

    addon = sys.modules.get(module)
    if addon is not None:
        deps = addon.deps
        # This is the point of the stage: the SDK lives in the add-on's own
        # directory, and nothing else here puts it on sys.path.
        check("the Rerun SDK survived the restart", deps.available(), deps.versions())
        print(f"  SDK: {deps.versions()}")
        print(f"  from: {deps.deps_dir()}")
        if deps.available():
            import tempfile

            import rerun as rr

            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "restart_check.rrd")
                rec = rr.RecordingStream("restart_check")
                rec.save(path)
                rec.set_time("t", duration=0.0)
                rec.log("/world/points", rr.Points3D(
                    positions=[[0, 0, 0], [1, 1, 1]], colors=[[9, 9, 9]] * 2))
                rec.flush()
                del rec
                result = bpy.ops.import_scene.rrd(filepath=path)
                check("importing still works after a restart",
                      result == {"FINISHED"}, result)
                check("the recording became objects",
                      any(o.name == "points" for o in bpy.data.objects),
                      [o.name for o in bpy.data.objects])

    return report()


def report():
    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print(f"  FAILED {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    sys.exit(main(args))
