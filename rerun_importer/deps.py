"""Locate — or install — the Rerun SDK that Blender needs to read .rrd files.

The rrd format is versioned and internal, so reading it means running Rerun's
own code.  Blender ships neither ``rerun-sdk`` nor ``pyarrow``, and both are
large (~200 MB together), which is too much to vendor in a git repository.
So the add-on looks for them in this order:

1. already importable (a system install, or wheels vendored into ``wheels/``);
2. the add-on's private ``_deps`` directory, from a previous install;
3. installed on request by ``rrd.install_dependencies``.

``rerun-sdk`` publishes abi3 wheels, so one wheel covers every Python that
Blender is likely to embed.

The install path is the fiddly part, because *Blender's Python may have no
installer at all*.  A distro build against the system interpreter (Arch's
Blender 5.x, say) has no ``pip`` module, and ``ensurepip`` refuses to bootstrap
one on a PEP 668 "externally managed" install.  Hence the ladder in
:func:`_installer_command`, which ends up running pip straight out of the wheel
that ``ensurepip`` carries — no bootstrap, nothing written outside the add-on.
"""

from __future__ import annotations

import glob
import os
import shutil
import site
import subprocess
import sys

REQUIREMENTS = ("rerun-sdk>=0.20", "pyarrow>=14")
_DEPS_DIRNAME = "_deps"


# ---------------------------------------------------------------------------
# finding what is already there
# ---------------------------------------------------------------------------


def deps_dir() -> str:
    """Where this add-on keeps its own copy of the SDK."""
    try:
        import bpy

        base = bpy.utils.extension_path_user(__package__, create=True)
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, _DEPS_DIRNAME)


def vendored_dir() -> str:
    """``wheels/``, next to this file — what ``make wheels`` fills in."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "wheels")


def ensure_on_path() -> bool:
    """Put every candidate location on ``sys.path``; True if rerun imports."""
    for candidate in (vendored_dir(), deps_dir()):
        if os.path.isdir(candidate):
            _add_site_dir(candidate)
    return available()


def _add_site_dir(path: str):
    """Add ``path`` to ``sys.path``, honouring any ``.pth`` files in it.

    ``rerun-sdk`` installs its package under ``rerun_sdk/`` and points a
    ``rerun_sdk.pth`` at it, so a plain ``sys.path`` insert finds nothing.
    ``site.addsitedir`` normally handles that, but it quietly does nothing when
    the directory is already registered — which happens after Blender reloads
    its scripts — so the ``.pth`` lines are also applied by hand.
    """
    if path not in sys.path:
        sys.path.insert(0, path)
    site.addsitedir(path)
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return
    for entry in entries:
        if not entry.endswith(".pth"):
            continue
        try:
            with open(os.path.join(path, entry), encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("import "):
                continue
            target = line if os.path.isabs(line) else os.path.join(path, line)
            if os.path.isdir(target) and target not in sys.path:
                sys.path.insert(0, target)


def available() -> bool:
    try:
        import pyarrow  # noqa: F401
        import rerun  # noqa: F401
    except Exception:
        return False
    return True


def versions() -> str:
    try:
        import pyarrow
        import rerun

        return f"rerun-sdk {rerun.__version__}, pyarrow {pyarrow.__version__}"
    except Exception:
        return "not installed"


# ---------------------------------------------------------------------------
# installing
# ---------------------------------------------------------------------------


def _find_uv() -> "str | None":
    """uv, if the user has it — by far the fastest way to fetch these wheels."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (
        "~/.local/bin/uv", "~/.cargo/bin/uv", "/usr/local/bin/uv", "/opt/homebrew/bin/uv",
    ):
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _bundled_pip_wheel() -> "str | None":
    """The pip wheel that ``ensurepip`` carries, if this Python has one.

    pip runs perfectly well from inside its own wheel — it is a zip on
    ``sys.path`` — which sidesteps bootstrapping pip into an externally
    managed interpreter entirely.
    """
    try:
        import ensurepip
    except ImportError:
        return None
    bundled = os.path.join(os.path.dirname(ensurepip.__file__), "_bundled")
    wheels = sorted(glob.glob(os.path.join(bundled, "pip-*.whl")))
    return wheels[-1] if wheels else None


def _installer_command():
    """How to install wheels for *this* Blender's Python: (argv, env)."""
    uv = _find_uv()
    if uv:
        return [uv, "pip", "install", "--python", sys.executable], {}

    try:
        import pip  # noqa: F401

        return [sys.executable, "-m", "pip", "install", "--no-input"], {}
    except ImportError:
        pass

    wheel = _bundled_pip_wheel()
    if wheel:
        pythonpath = os.pathsep.join(
            p for p in (wheel, os.environ.get("PYTHONPATH", "")) if p
        )
        return (
            [sys.executable, "-m", "pip", "install", "--no-input"],
            # A --target install does not touch the interpreter, but say so
            # anyway: some distro pip builds refuse on principle otherwise.
            {"PYTHONPATH": pythonpath, "PIP_BREAK_SYSTEM_PACKAGES": "1"},
        )

    raise RuntimeError(
        "This Blender's Python has no pip and nothing to bootstrap one from.\n"
        "Install uv (https://docs.astral.sh/uv/), or install the wheels by hand:\n"
        f"  python -m pip install --target '{deps_dir()}' " + " ".join(REQUIREMENTS)
    )


def install(target: "str | None" = None, upgrade: bool = False) -> str:
    """Install the SDK into the add-on's private directory.

    Returns the installer's combined output.  Raises ``RuntimeError`` on
    failure, so the operator can put the reason in front of the user instead of
    leaving it in a console they may never open.
    """
    target = target or deps_dir()
    os.makedirs(target, exist_ok=True)

    argv, extra_env = _installer_command()
    command = [*argv, "--target", target, *REQUIREMENTS]
    if upgrade:
        command.append("--upgrade")
    env = dict(os.environ)
    env.update(extra_env)

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=1800, env=env
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(f"could not run the installer: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"the installer timed out after 30 minutes: {exc}") from exc

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(
            f"{os.path.basename(command[0])} failed:\n" + (output.strip()[-2000:] or "?")
        )

    _forget_failed_imports()
    _add_site_dir(target)
    if not available():
        raise RuntimeError(
            "the installer reported success but the SDK still will not import:\n"
            + output[-2000:]
        )
    return output


def _forget_failed_imports():
    """Drop half-imported modules so a retry after installing actually works."""
    for prefix in ("rerun", "pyarrow"):
        for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
            sys.modules.pop(name, None)
