#!/usr/bin/env python3
"""AndroidForge — Build Project. Reads detection + setup JSON and runs the appropriate build command(s) for the project. The script is non-destructive: it never modifies source files, only runs commands in the project directory. Strategy: 1. If Flutter → `flutter pub get` then `flutter build apk`. 2. Otherwise → run gradle tasks (assembleDebug, assembleRelease, bundleDebug) in order until one succeeds. Stop on first success. 3. If the gradle wrapper is missing/corrupt, regenerate it with `gradle wrapper --gradle-version <X>` first. 4. On success, write the produced command + log path to GITHUB_OUTPUT. 5. On failure, exit non-zero so the workflow runs the diagnosis step. The build is run inside the project root directory and inherits the runner's PATH (which includes the JDK and Gradle installed by previous workflow steps). """
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run(cmd: list[str], cwd: Path, log_file: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a command, stream stdout/stderr to console AND a log file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Running: {' '.join(cmd)}", flush=True)
    print(f" cwd: {cwd}", flush=True)
    print(f" log: {log_file}", flush=True)
    start = time.time()
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    with log_file.open("w", encoding="utf-8", errors="replace") as f:
        f.write(f"$ {' '.join(cmd)}\n")
        f.write(f"# cwd: {cwd}\n\n")
        f.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            f.write(line)
        rc = proc.wait()
    elapsed = time.time() - start
    print(f"=== Exit {rc} after {elapsed:.1f}s", flush=True)
    return rc, str(log_file)


def maybe_regenerate_wrapper(project_root: Path, gradle_version: str | None, log_dir: Path) -> bool:
    """If gradle-wrapper.jar is missing, run `gradle wrapper` to regenerate."""
    jar = project_root / "gradle" / "wrapper" / "gradle-wrapper.jar"
    if jar.exists():
        return True
    if not gradle_version:
        print("WARNING: cannot regenerate wrapper — no Gradle version specified", flush=True)
        return False
    cmd = ["gradle", "wrapper", "--gradle-version", gradle_version, "--distribution-type", "bin"]
    rc, _ = run(cmd, project_root, log_dir / "regenerate-wrapper.log")
    return rc == 0 and jar.exists()



def has_package_artifact(root: Path) -> bool:
    """Return True only when a real APK/AAB exists after a build command."""
    ignored = {"intermediates", ".gradle", ".git", "node_modules", ".dart_tool", ".pub-cache", "caches"}
    roots = [root]
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        workspace_path = Path(workspace).resolve()
        if workspace_path not in roots:
            roots.append(workspace_path)
    for scan_root in roots:
        if not scan_root.exists():
            continue
        try:
            for path in scan_root.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".apk", ".aab"}:
                    if not any(part.lower() in ignored for part in path.parts):
                        return True
        except Exception as exc:
            print(f"WARNING: artifact validation scan failed for {scan_root}: {exc}", flush=True)
    return False

def main() -> int:
    parser = argparse.ArgumentParser(description="AndroidForge build project")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--detect", required=True, help="Detection JSON (string or path)")
    parser.add_argument("--toolchain", required=True, help="Toolchain JSON (string or path)")
    parser.add_argument("--variant", default="auto", choices=["auto", "debug", "release", "bundle"])
    parser.add_argument("--log-dir", default=None, help="Directory to write logs")
    parser.add_argument("--output", default=None, help="Write JSON summary to this file")
    args = parser.parse_args()

    def load(s: str) -> Any:
        if Path(s).exists():
            return json.loads(Path(s).read_text())
        return json.loads(s)

    detect = load(args.detect)
    toolchain = load(args.toolchain)

    root = Path(args.root).resolve()
    log_dir = Path(args.log_dir) if args.log_dir else root.parent / "androidforge-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    project_type = detect.get("project_type", "unknown")
    needs_gradle = toolchain.get("needs_gradle", False)
    needs_flutter = toolchain.get("needs_flutter", False)
    use_wrapper = toolchain.get("use_wrapper", False)

    # Filter build commands based on requested variant
    all_commands = toolchain.get("build_commands", [])
    prep_commands = toolchain.get("prep_commands", []) or []
    if args.variant != "auto":
        # Filter by requested variant
        filtered = []
        for cmd in all_commands:
            cmd_str = " ".join(cmd).lower()
            if args.variant == "debug" and "debug" in cmd_str:
                filtered.append(cmd)
            elif args.variant == "release" and "release" in cmd_str:
                filtered.append(cmd)
            elif args.variant == "bundle" and "bundle" in cmd_str:
                filtered.append(cmd)
        if filtered:
            all_commands = filtered

    # If we use gradlew, regenerate wrapper if missing
    if use_wrapper and project_type != "flutter":
        gradlew = root / "gradlew"
        if not gradlew.exists():
            print("WARNING: gradlew missing despite detection — regenerating via system gradle", flush=True)
            maybe_regenerate_wrapper(root, toolchain.get("gradle_version"), log_dir)

    # Determine env for build
    build_env: dict[str, str] = {}
    if needs_flutter:
        # Flutter requires Java 17 for Android builds
        pass

    # ---- Run prep commands first ----
    # These are commands like `flutter pub get` that must run before the
    # actual build, but whose exit code does NOT determine whether the
    # build succeeded (they don't produce an APK — they just download
    # dependencies). We log their output but never let their success
    # short-circuit the build_commands loop below.
    if prep_commands:
        print(f"\n=== Running {len(prep_commands)} prep command(s) — these do not count as build success ===", flush=True)
    for cmd in prep_commands:
        # Apply the same gradlew sh-wrap if needed (rare for prep, but safe)
        if use_wrapper and cmd and cmd[0].endswith("gradlew"):
            gradlew_path = Path(cmd[0])
            if gradlew_path.exists():
                try:
                    gradlew_path.chmod(gradlew_path.stat().st_mode | 0o111)
                except Exception:
                    pass
                cmd = ["/bin/sh", str(gradlew_path)] + cmd[1:]
        rc, log = run(cmd, root, log_dir / f"prep-{int(time.time())}-{len(cmd)}.log", build_env)
        if rc != 0:
            # Prep failure is a warning, not a build failure — the build
            # commands might still succeed (e.g. dependencies cached).
            print(f"⚠ prep command failed (rc={rc}): {' '.join(cmd)}", flush=True)
        else:
            print(f"✓ prep command ok: {' '.join(cmd)}", flush=True)

    # ---- Run build commands (stop on first success) ----
    success = False
    last_rc = 1
    last_log: str = ""
    successful_command: list[str] | None = None

    for cmd in all_commands:
        # For gradlew commands: ensure the file is executable AND invoke it
        # via `sh` to bypass any remaining exec-bit issues (Python's
        # zipfile extraction sometimes loses the +x bit even after chmod).
        if use_wrapper and cmd and cmd[0].endswith("gradlew"):
            gradlew_path = Path(cmd[0])
            if gradlew_path.exists():
                try:
                    gradlew_path.chmod(gradlew_path.stat().st_mode | 0o111)
                except Exception:
                    pass
                # Replace ["/path/to/gradlew", "assembleDebug"] with
                # ["/bin/sh", "/path/to/gradlew", "assembleDebug"]
                # This makes the build resilient to filesystems that
                # silently drop the exec bit.
                cmd = ["/bin/sh", str(gradlew_path)] + cmd[1:]
        rc, log = run(cmd, root, log_dir / f"build-{int(time.time())}-{len(cmd)}.log", build_env)
        last_rc = rc
        last_log = log
        if rc == 0:
            if has_package_artifact(root):
                success = True
                successful_command = cmd
                print("✓ Build command succeeded and an APK/AAB artifact exists.", flush=True)
                break
            print(
                "⚠ Build command returned success, but no APK/AAB was found yet; "
                "trying the next configured build command.",
                flush=True,
            )

    # Output JSON summary
    result = {
        "project_root": str(root),
        "project_type": project_type,
        "build_succeeded": success,
        "successful_command": successful_command,
        "exit_code": last_rc,
        "log_file": last_log,
        "prep_commands_run": prep_commands,
        "build_commands_attempted": all_commands,
        "artifact_found": has_package_artifact(root) if success else False,
    }
    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output)
    else:
        print(output)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"build_succeeded={'true' if success else 'false'}\n")
            f.write(f"exit_code={last_rc}\n")
            f.write(f"log_file={last_log}\n")
            if successful_command:
                f.write(f"successful_command={' '.join(successful_command)}\n")
            f.write(f"json<<EOF\n{json.dumps(result)}\nEOF\n")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
