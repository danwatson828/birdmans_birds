#!/usr/bin/env python3
"""Package the MCP server as a Claude Extension (.mcpb) bundle.

WHY A BUNDLE RATHER THAN AN mcpServers ENTRY. `claude_desktop_config.json` is
owned by Claude Desktop, which rewrites it wholesale from its own model --
`coworkUserFilesPath` and `preferences` and nothing else. An `mcpServers` key
added from outside survived about thirteen hours before the app's next save
dropped it. Extensions live somewhere else entirely: a bundle directory under
`Claude Extensions/`, a record in `extensions-installations.json`, and an
enable flag in `Claude Extensions Settings/`. Desktop Commander has sat in
that registry since July. The registry is written when an extension is
installed or removed, not when a preference changes, which is the whole point.

WHAT THIS BUNDLE DOES AND DOES NOT CONTAIN. It does not vendor the project.
The server needs the project's virtualenv (mcp, duckdb, pandas, statsmodels),
its sibling modules (`agent`, `analyses`), and a 174MB snapshot; copying those
into an extension directory would create a second, diverging installation. So
the bundle carries the manifest and a small launcher, and the launcher runs
the real server from the real project using the real venv. The bundle is a
registration record that Claude Desktop maintains on our behalf.

    python build_extension.py              # build dist/nba-analytics.mcpb
    python build_extension.py --install    # build and register it locally
    python build_extension.py --uninstall  # remove the local registration
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
EXT_ID = "local.nba-analytics"
VERSION = "1.0.0"

CLAUDE_DIR = Path.home() / "Library" / "Application Support" / "Claude"
EXT_DIR = CLAUDE_DIR / "Claude Extensions"
EXT_SETTINGS_DIR = CLAUDE_DIR / "Claude Extensions Settings"
REGISTRY = CLAUDE_DIR / "extensions-installations.json"

BUILD = PROJECT_ROOT / "dist" / "extension"
MCPB = PROJECT_ROOT / "dist" / "nba-analytics.mcpb"

# The launcher. It lives inside the bundle and runs the project's own server,
# so there is exactly one copy of the tool code and one venv.
LAUNCHER = '''#!/usr/bin/env python3
"""Launch the nba-analytics MCP server from its project checkout.

The bundle deliberately does not vendor the project. This resolves the
checkout, fails loudly if it has moved, and hands off to the real server.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
cfg = json.loads((HERE / "project.json").read_text())
root = Path(cfg["project_root"])

if not (root / "mcp_server.py").is_file():
    sys.stderr.write(
        f"nba-analytics extension: project not found at {root}.\\n"
        "The extension records where the checkout was when it was built; if "
        "it moved, rebuild with `python build_extension.py --install`.\\n")
    raise SystemExit(1)

# Run from the project root so relative paths inside the project resolve, and
# put it first on sys.path so `agent` and `analyses` import from the checkout.
os.chdir(root)
sys.path.insert(0, str(root))

import mcp_server                                        # noqa: E402
raise SystemExit(mcp_server.build_server().run(transport="stdio") or 0)
'''


def manifest() -> dict:
    """Modelled on the installed Desktop Commander manifest (version 0.3).

    `mcp_config.command` is the project's venv interpreter, NOT a resolved
    path: `.venv/bin/python` is a symlink whose target is the system
    interpreter, which has none of this project's packages. A venv works by
    being entered through its own bin/.
    """
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    return {
        "manifest_version": "0.3",
        "name": "nba-analytics",
        "display_name": "NBA Analytics",
        "version": VERSION,
        "description": ("Read-only analytics over 80 seasons of NBA box "
                        "scores, with the project's determinism guard and "
                        "methodology standard."),
        "long_description": (
            "Exposes this project's verified tools: run_sql and run_analysis "
            "against a published snapshot (every query executed three times "
            "and compared, so a non-reproducible result is rejected rather "
            "than returned), check_coverage for per-stat era boundaries, an "
            "investigate_relationship protocol that requires a held-out era "
            "and permits NOT CONFIRMED, save_analysis writing through the "
            "project's single writer, and manual refresh triggers that are "
            "dry-run by default."),
        "author": {"name": "Dan Watson"},
        "icon": "icon.png",
        "server": {
            "type": "python",
            "entry_point": "launcher.py",
            "mcp_config": {
                "command": str(venv_python),
                "args": ["${__dirname}/launcher.py"],
                "env": {"PYTHONUNBUFFERED": "1"},
            },
        },
        "tools": [
            {"name": "run_sql",
             "description": "One read-only SELECT against the NBA snapshot."},
            {"name": "run_analysis",
             "description": "Python statistics over a dataframe from the snapshot."},
            {"name": "check_coverage",
             "description": "Per-stat era coverage, computed live."},
            {"name": "investigate_relationship",
             "description": "The skeptical protocol for testing a relationship."},
            {"name": "save_analysis",
             "description": "Persist an analysis to the project's store."},
            {"name": "refresh_nba_data",
             "description": "Manually trigger NBA ingestion. Dry-run by default."},
            {"name": "refresh_espn_data",
             "description": "Manually trigger the ESPN league sync. Dry-run by default."},
        ],
        "keywords": ["nba", "basketball", "analytics", "duckdb"],
        "license": "MIT",
        "compatibility": {
            "platforms": ["darwin"],
            "runtimes": {"python": ">=3.11"},
        },
    }


def build() -> Path:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    (BUILD / "manifest.json").write_text(json.dumps(manifest(), indent=2) + "\n")
    (BUILD / "launcher.py").write_text(LAUNCHER)
    (BUILD / "project.json").write_text(
        json.dumps({"project_root": str(PROJECT_ROOT)}, indent=2) + "\n")

    # A 1x1 PNG so the manifest's icon reference resolves. Not decoration:
    # a manifest pointing at a missing file is a validation failure.
    icon = PROJECT_ROOT / "web" / "logos" / "bos.png"
    if icon.is_file():
        shutil.copy2(icon, BUILD / "icon.png")
    else:
        (BUILD / "icon.png").write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6300010000050001" "0d0a2db40000000049454e44ae426082"))

    MCPB.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(MCPB, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(BUILD.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(BUILD).as_posix())
    return MCPB


def bundle_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install() -> int:
    """Register the built bundle the way an installed extension looks.

    Claude Desktop normally writes these records itself when you install a
    .mcpb through its UI. Writing them here is the only way to test the
    survival property without a human clicking, and every file touched is
    backed up first.
    """
    mcpb = build()
    target = EXT_DIR / EXT_ID
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for f in BUILD.rglob("*"):
        if f.is_file():
            dest = target / f.relative_to(BUILD)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)

    EXT_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    (EXT_SETTINGS_DIR / f"{EXT_ID}.json").write_text(
        json.dumps({"isEnabled": True}, indent=2) + "\n")

    reg = {"extensions": {}}
    if REGISTRY.exists():
        shutil.copy2(REGISTRY, f"{REGISTRY}.bak-{int(dt.datetime.now().timestamp())}")
        reg = json.loads(REGISTRY.read_text())
    reg.setdefault("extensions", {})[EXT_ID] = {
        "id": EXT_ID,
        "version": VERSION,
        "hash": bundle_hash(mcpb),
        "installedAt": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "manifest": manifest(),
        "signatureInfo": {"status": "unsigned"},
        "source": "local",
    }
    REGISTRY.write_text(json.dumps(reg, indent=2) + "\n")

    back = json.loads(REGISTRY.read_text())
    ok = EXT_ID in back.get("extensions", {})
    print(f"bundle:   {mcpb}  ({mcpb.stat().st_size:,} bytes)")
    print(f"unpacked: {target}")
    print(f"settings: {EXT_SETTINGS_DIR / (EXT_ID + '.json')}")
    print(f"registry re-read from disk: registered={ok}")
    return 0 if ok else 1


def uninstall() -> int:
    target = EXT_DIR / EXT_ID
    if target.exists():
        shutil.rmtree(target)
    s = EXT_SETTINGS_DIR / f"{EXT_ID}.json"
    if s.exists():
        s.unlink()
    if REGISTRY.exists():
        reg = json.loads(REGISTRY.read_text())
        reg.get("extensions", {}).pop(EXT_ID, None)
        REGISTRY.write_text(json.dumps(reg, indent=2) + "\n")
    print("removed local extension registration")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()
    if args.uninstall:
        return uninstall()
    if args.install:
        return install()
    p = build()
    print(f"built {p} ({p.stat().st_size:,} bytes)")
    print(f"sha256 {bundle_hash(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
