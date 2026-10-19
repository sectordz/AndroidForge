#!/usr/bin/env python3
"""AndroidForge — APK Collection. After the build step completes, search the project tree for generated APK/AAB files, select the most relevant one, rename it clearly, and copy it into a clean output directory ready for `actions/upload-artifact`. Selection priority (highest first): 1. `*-release.apk` (release builds preferred over debug) 2. `*-debug.apk` 3. any other `*.apk` 4. `*.aab` (app bundle) as fallback Files in `build/intermediates/` are skipped — those are intermediate outputs. """
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def is_relevant_apk(path: Path, output_dir: Path | None = None) -> bool:
    """Ignore intermediate/cache files and the collector's own output."""
    try:
        resolved = path.resolve()
        if output_dir is not None and (resolved == output_dir.resolve() or output_dir.resolve() in resolved.parents):
            return False
    except Exception:
        pass
    ignored = {"intermediates", ".gradle", ".git", "node_modules", ".dart_tool", ".pub-cache", "caches"}
    return not any(part.lower() in ignored for part in path.parts)


def find_apks(root: Path, extra_roots: list[Path] | None = None, output_dir: Path | None = None) -> list[Path]:
    """Search the project and workspace, because some project layouts emit outputs in sibling folders."""
    candidates: set[Path] = set()
    roots = [root]
    if extra_roots:
        roots.extend(extra_roots)
    for scan_root in roots:
        if not scan_root.exists() or not scan_root.is_dir():
            print(f"WARNING: APK search root does not exist: {scan_root}", file=sys.stderr)
            continue
        try:
            for p in scan_root.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in {".apk", ".aab"}:
                    continue
                if is_relevant_apk(p, output_dir):
                    candidates.add(p.resolve())
        except Exception as e:
            print(f"WARNING: error while scanning {scan_root} for APKs: {e}", file=sys.stderr)
    return sorted(candidates, key=lambda p: str(p))


def priority(p: Path) -> int:
    name = p.name.lower()
    if name.endswith(".aab"):
        return 100
    if "release" in name:
        return 1
    if "debug" in name:
        return 2
    return 5


def select_primary(apks: list[Path], preferred_variant: str | None = None) -> Path | None:
    if not apks:
        return None
    if preferred_variant:
        v = preferred_variant.lower()
        preferred = [p for p in apks if v in p.name.lower()]
        if preferred:
            return sorted(preferred, key=priority)[0]
    return sorted(apks, key=priority)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="AndroidForge APK collection")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--variant", default="auto", choices=["auto", "debug", "release", "bundle"])
    parser.add_argument("--output-dir", default=None, help="Where to copy selected APKs (default: <repo>/output)")
    parser.add_argument("--output-json", default=None, help="Write JSON summary to this file")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output_dir = Path(args.output_dir) if args.output_dir else (Path(__file__).resolve().parent.parent / "output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clean output dir
    for p in output_dir.glob("*.apk"):
        p.unlink()
    for p in output_dir.glob("*.aab"):
        p.unlink()

    # Search the extracted project first, then the repository workspace as a
    # fallback for projects whose build scripts write outputs outside project_root.
    extra_roots: list[Path] = []
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        extra_roots.append(Path(workspace).resolve())
    apks = find_apks(root, extra_roots=extra_roots, output_dir=output_dir)
    print(f"APK collector searched: {[str(root), *[str(p) for p in extra_roots]]}", flush=True)
    print(f"APK/AAB candidates found: {len(apks)}", flush=True)
    for candidate in apks:
        print(f" candidate: {candidate}", flush=True)
    preferred = None
    if args.variant != "auto":
        preferred = args.variant

    primary = select_primary(apks, preferred)

    collected: list[dict[str, str]] = []
    for apk in apks:
        # Generate a clean, unique output filename
        suffix = ".aab" if apk.name.endswith(".aab") else ".apk"
        stem = apk.stem
        # Strip variant suffix to avoid duplication
        for v in ("release", "debug"):
            stem = stem.replace(f"-{v}", "").replace(f"_{v}", "")
        out_name = f"AndroidForge-{stem}-{apk.parent.name}{suffix}"
        out_path = output_dir / out_name
        try:
            shutil.copy2(apk, out_path)
        except Exception as e:
            print(f"WARNING: failed to copy {apk} → {out_path}: {e}", file=sys.stderr)
            continue
        size_kb = out_path.stat().st_size / 1024
        collected.append({
            "source_path": str(apk),
            "output_path": str(out_path),
            "output_name": out_name,
            "size_kb": round(size_kb, 1),
            "is_primary": apk == primary,
        })

    primary_info = next((c for c in collected if c["is_primary"]), None)

    result: dict[str, Any] = {
        "project_root": str(root),
        "apk_count": len(collected),
        "primary": primary_info,
        "all_apks": collected,
        "output_directory": str(output_dir),
        "search_roots": [str(root), *[str(p) for p in extra_roots]],
    }
    output = json.dumps(result, indent=2)
    if args.output_json:
        Path(args.output_json).write_text(output)
    else:
        print(output)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"apk_count={len(collected)}\n")
            f.write(f"apk_directory={output_dir}\n")
            if primary_info:
                f.write(f"primary_apk={primary_info['output_path']}\n")
                f.write(f"primary_apk_name={primary_info['output_name']}\n")
            f.write(f"json<<EOF\n{json.dumps(result)}\nEOF\n")

    if not collected:
        print("ERROR: No APK/AAB was found. Check the Build project log for the actual build command and output path.", file=sys.stderr)
    return 0 if collected else 1


if __name__ == "__main__":
    sys.exit(main())
