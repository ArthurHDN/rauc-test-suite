#!/usr/bin/env python3
"""Run the RAUC integration tests and keep an auditable, reproducible result directory.

The tests are hardware independent; the device under test is selected with ``--device`` (or ``DEVICE`` in
the environment / .env) and handled by a plugin in ``libraries/devices/`` (see DEVICES.md).

Examples::

    ./run_tests.py --list-devices                    # supported devices
    ./run_tests.py --device raspberrypi4-64          # profile "safe", settings from .env, files from ./inputs
    ./run_tests.py --profile update --inputs /path/to/qa-inputs     # device from DEVICE in .env
    ./run_tests.py --device jetson-orin-nano-devkit-nvme --profile update --bundle /path/to/other.raucb
    ./run_tests.py --host 192.168.1.50 --user root --ask-password
    ./run_tests.py --profile safe -- -t "TC-2.*"     # everything after -- goes to Robot Framework
    ./run_tests.py --dry-run                         # parse suites only, no device access

Every run creates ``results/<UTC timestamp>[_label]/`` (``results/latest`` points to the newest one),
see README.md for the layout.  Passwords are never written to disk or put on a command line.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import importlib.metadata as md
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _ensure_environment() -> None:
    """Use ./.venv automatically when the interpreter lacks the test dependencies."""
    import importlib.util

    if all(importlib.util.find_spec(m) for m in ("robot", "robotcode", "paramiko")):
        return
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.exit("error: robotframework, robotcode and paramiko are required. Create the environment first:\n"
             f"  cd {ROOT} && uv venv && uv pip install --python .venv/bin/python -r requirements.txt\n"
             "  (or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)")


_ensure_environment()
sys.path.insert(0, str(ROOT / "libraries"))

from audit_common import DEFAULT_ENV_FILE, env_file_path, write_sha256sums  # noqa: E402

SECRET_KEYS = {"TARGET_PASSWORD", "TARGET_KEY_PASSPHRASE", "JETSON_PASSWORD", "JETSON_KEY_PASSPHRASE"}
TAR_EXCLUDES = {".venv", "results", "inputs", ".env", "__pycache__", ".pytest_cache"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", help=f"settings file (default: {DEFAULT_ENV_FILE})")
    p.add_argument("--device", help="device under test (DEVICE), see --list-devices; = Yocto MACHINE name")
    p.add_argument("--list-devices", action="store_true", help="list the supported devices and exit")
    p.add_argument("--host", help="target IP / hostname (TARGET_HOST)")
    p.add_argument("--user", help="SSH user (TARGET_USER, default root)")
    p.add_argument("--port", help="SSH port (TARGET_PORT, default 22)")
    p.add_argument("--key-file", help="SSH private key (TARGET_KEY_FILE)")
    p.add_argument("--ask-password", action="store_true",
                   help="prompt for the SSH password (just press Enter for an empty password)")
    p.add_argument("--profile", default=None, help="robot.toml profile: safe (default), update, acceptance, full")
    p.add_argument("--inputs", default=str(ROOT / "inputs"),
                   help="directory with all input files: bundle/, expected/, negative/ (default: ./inputs)")
    p.add_argument("--bundle", help="RAUC bundle (.raucb) under test; overrides <inputs>/bundle/*.raucb")
    p.add_argument("--bundle-archive", choices=["link", "copy", "none"], default="link",
                   help="how *.raucb inputs are preserved in the results (hardlink, copy, or only their hash)")
    p.add_argument("--results-root", default=str(ROOT / "results"), help="parent directory of result directories")
    p.add_argument("--label", help="suffix for the result directory name")
    p.add_argument("-v", "--variable", action="append", default=[], metavar="NAME:VALUE",
                   help="Robot variable, e.g. -v SOAK_CYCLES:2 (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="validate the suites without running them")
    p.add_argument("robot_args", nargs="*", help="extra Robot Framework options; put them after --")
    return p.parse_args(argv)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def run_capture(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        return r.stdout
    except OSError:
        return ""


def git_info() -> dict:
    """Git state of the test sources; unavailable when the folder is shipped without git."""
    top = run_capture(["git", "rev-parse", "--show-toplevel"], ROOT).strip()
    if not top:
        return {"available": False}
    return {
        "available": True,
        "commit": run_capture(["git", "rev-parse", "HEAD"], ROOT).strip(),
        "branch": run_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], ROOT).strip(),
        "dirty": bool(run_capture(["git", "status", "--porcelain", "--untracked-files=no", "--", "."], ROOT).strip()),
    }


def redact_env_file(path: Path) -> str:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        key, sep, val = raw.partition("=")
        k = key.strip().removeprefix("export ").strip()
        if sep and k in SECRET_KEYS:
            shown = "<empty>" if not val.strip().strip("\"'") else "<set, redacted>"
            out.append(f"{key}={shown}")
        else:
            out.append(raw)
    return "\n".join(out) + "\n"


def snapshot_inputs(run_dir: Path, env_file: Path, git: dict) -> None:
    inputs = run_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)

    def tar_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(ti.name).parts
        return None if any(part in TAR_EXCLUDES or part.endswith(".pyc") for part in parts) else ti

    with tarfile.open(inputs / "rauc-tests-sources.tar.gz", "w:gz") as tf:
        tf.add(ROOT, arcname="rauc-tests", filter=tar_filter)

    if env_file.is_file():
        (inputs / "env.redacted").write_text(redact_env_file(env_file), encoding="utf-8")
    if git.get("available"):
        diff = run_capture(["git", "diff", "HEAD", "--", "."], ROOT)
        if diff:
            (inputs / "git-diff.patch").write_text(diff, encoding="utf-8")
        (inputs / "git-status.txt").write_text(run_capture(["git", "status", "--short", "--", "."], ROOT), encoding="utf-8")
    packages = sorted(f"{d.metadata['Name']}=={d.version}" for d in md.distributions() if d.metadata["Name"])
    (inputs / "python-packages.txt").write_text("\n".join(packages) + "\n", encoding="utf-8")


def collect_results(output_xml: Path) -> list[dict]:
    from robot.api import ExecutionResult, ResultVisitor

    tests: list[dict] = []

    class Collector(ResultVisitor):
        def visit_test(self, test):  # noqa: ANN001
            m = re.match(r"(TC-\d+\.\d+)", test.name)
            tests.append({
                "id": m.group(1) if m else "",
                "name": test.name,
                "suite": test.parent.full_name,
                "status": test.status,
                "message": (test.message or "").strip(),
                "duration_s": round(test.elapsed_time.total_seconds(), 1),
                "tags": sorted(str(t) for t in test.tags),
            })

    ExecutionResult(str(output_xml)).visit(Collector())
    return tests


def md_escape(text: str, limit: int = 160) -> str:
    one = " ".join(text.split())
    one = one if len(one) <= limit else one[: limit - 3] + "..."
    return one.replace("|", "\\|")


def write_result_sheet(run_dir: Path, tests: list[dict], manifest: dict) -> None:
    from audit_common import test_dir_name

    counts = manifest["counts"]
    t = manifest["target"]
    lines = [
        f"# RAUC test result sheet - {manifest['run_id']}",
        "",
        f"- Device: `{manifest['device'] or 'n/a'}`",
        f"- Target: `{t['user']}@{t['host']}:{t['port']}` (password {t['password']})",
        f"- Profile: `{manifest['profile']}`",
        f"- Started / finished (UTC): {manifest['started_utc']} / {manifest['finished_utc']}",
        f"- Git (test sources): `{manifest['git'].get('commit', 'n/a')}`{' (dirty, see inputs/git-diff.patch)' if manifest['git'].get('dirty') else ''}",
        f"- Inputs directory: `{manifest['inputs']['dir']}` ({len(manifest['inputs']['files'])} files, see inputs/test-inputs/inputs-manifest.json)",
        f"- Bundle: {manifest.get('bundle') or 'n/a'}",
        f"- Totals: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['SKIP']} SKIP, {counts['total']} total",
        "",
        "| Test | Title | Result | Time (s) | Evidence | Message |",
        "|------|-------|--------|---------:|----------|---------|",
    ]
    for tc in tests:
        title = tc["name"][len(tc["id"]):].strip() if tc["id"] else tc["name"]
        evidence = f"artifacts/{test_dir_name(tc['name'])}/"
        lines.append(f"| {tc['id']} | {md_escape(title, 80)} | {tc['status']} | {tc['duration_s']} | "
                     f"`{evidence}` | {md_escape(tc['message'])} |")
    lines += [
        "",
        "Verify integrity with `sha256sum -c SHA256SUMS` (whole run) or inside any `artifacts/<test>/` directory.",
        "Reproduce with `reproduce.sh`.",
        "",
    ]
    (run_dir / "RESULT-SHEET.md").write_text("\n".join(lines), encoding="utf-8")


def write_reproduce(run_dir: Path, argv: list[str], manifest: dict) -> None:
    archived = manifest["inputs"]["archived"]
    kept: list[str] = []
    skip = False
    for a in argv:
        if not archived:
            kept.append(a)
        elif skip:
            skip = False
        elif a in ("--bundle", "--inputs"):
            skip = True
        elif not a.startswith(("--bundle=", "--inputs=")):
            kept.append(a)
    inputs_arg = ' --inputs "$RUN_DIR/inputs/test-inputs"' if archived else ""
    git = manifest["git"]
    t = manifest["target"]
    text = f"""#!/usr/bin/env bash
# Reproduce run {manifest['run_id']}.
#  - Secrets are NOT stored. Export TARGET_PASSWORD (empty is fine) or add --ask-password.
#  - Override TARGET_HOST / TARGET_USER to test another unit of the same device (this run used
#    {t['user']}@{t['host']}:{t['port']}, device {manifest['device'] or 'n/a'}).
#  - Test sources exactly as used: inputs/rauc-tests-sources.tar.gz (extract and set TESTS_DIR to it);
#    git commit {git.get('commit', 'n/a')}{' (+ uncommitted changes in inputs/git-diff.patch)' if git.get('dirty') else ''}.
#  - Input files (bundle, expected files, negative bundles) are taken from inputs/test-inputs/ when archived.
set -euo pipefail
RUN_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
TESTS_DIR="${{TESTS_DIR:-{ROOT}}}"
export TARGET_HOST="${{TARGET_HOST:-{t['host']}}}"
export TARGET_USER="${{TARGET_USER:-{t['user']}}}"
export DEVICE="${{DEVICE:-{manifest['device'] or ''}}}"
exec "${{PYTHON:-python3}}" "$TESTS_DIR/run_tests.py"{inputs_arg} {shlex.join(kept)}
"""
    p = run_dir / "reproduce.sh"
    p.write_text(text, encoding="utf-8")
    p.chmod(0o755)


def main() -> int:
    ns = parse_args(sys.argv[1:])
    argv = sys.argv[1:]

    from devices import list_devices

    if ns.list_devices:
        for name, description in list_devices():
            print(f"{name:36} {description}")
        return 0

    # ---- settings --------------------------------------------------------------------------
    if ns.env_file:
        os.environ["RAUC_TESTS_ENV_FILE"] = str(Path(ns.env_file).resolve())
    if ns.device:
        os.environ["DEVICE"] = ns.device
    for flag, var in (("host", "TARGET_HOST"), ("user", "TARGET_USER"), ("port", "TARGET_PORT"),
                      ("key_file", "TARGET_KEY_FILE")):
        if getattr(ns, flag) is not None:
            os.environ[var] = str(getattr(ns, flag))
    if ns.ask_password:
        os.environ["TARGET_PASSWORD"] = getpass.getpass("SSH password (empty allowed): ")

    from audit_common import get_setting

    env_file = env_file_path()
    device = get_setting("DEVICE")
    known = dict(list_devices())
    if device and device not in known:
        print(f"error: unknown device '{device}'. Supported: {', '.join(known)} (see --list-devices, DEVICES.md)",
              file=sys.stderr)
        return 2
    if not device and not ns.dry_run:
        print("error: no device selected. Use --device <name> or set DEVICE in .env. Supported: "
              + ", ".join(known), file=sys.stderr)
        return 2
    host = get_setting("TARGET_HOST")
    if not host and not ns.dry_run:
        print("error: TARGET_HOST is not set. Copy env.example to .env and edit it, or use --host.", file=sys.stderr)
        return 2
    user = get_setting("TARGET_USER", "root") or "root"
    port = get_setting("TARGET_PORT", "22") or "22"
    pw = get_setting("TARGET_PASSWORD")
    pw_state = "unset (empty password / key login)" if pw is None else ("empty" if pw == "" else "set")

    # ---- inputs ----------------------------------------------------------------------------
    inputs_dir = Path(ns.inputs).expanduser().resolve()
    if ns.bundle and not Path(ns.bundle).expanduser().is_file():
        print(f"error: bundle not found: {ns.bundle}", file=sys.stderr)
        return 2
    if not inputs_dir.is_dir():
        print(f"warning: inputs directory not found: {inputs_dir} (tests that need input files will fail or skip)",
              file=sys.stderr)

    # ---- result directory ------------------------------------------------------------------
    started = utc_now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + (f"_{re.sub(r'[^A-Za-z0-9._-]+', '-', ns.label)}" if ns.label else "")
    results_root = Path(ns.results_root).resolve()
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    latest = results_root / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run_dir.name)
    except OSError:
        pass

    git = git_info()
    print(f"Result directory: {run_dir}")
    snapshot_inputs(run_dir, env_file, git)

    # ---- run Robot Framework ---------------------------------------------------------------
    profile = ns.profile or "safe"
    cmd = [sys.executable, "-m", "robotcode.cli", "--profile", profile, "robot",
           "-d", str(run_dir), "-o", "output.xml", "-l", "log.html", "-r", "report.html",
           "-b", "debug.log", "-x", "xunit.xml",
           "-v", f"BUNDLE_ARCHIVE_MODE:{ns.bundle_archive}",
           "-v", f"INPUTS_DIR:{inputs_dir}"]
    if device:
        cmd += ["-v", f"DEVICE:{device}"]
    if ns.bundle:
        cmd += ["-v", f"BUNDLE_PATH:{Path(ns.bundle).expanduser().resolve()}"]
    for var in ns.variable:
        cmd += ["-v", var]
    if ns.dry_run:
        cmd.append("--dryrun")
    cmd += [a for a in ns.robot_args if a != "--"]

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    (run_dir / "command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
    print("Running:", shlex.join(cmd))
    rc = 1
    with open(run_dir / "console.log", "w", encoding="utf-8") as console:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                console.write(line)
        except KeyboardInterrupt:
            proc.terminate()
        rc = proc.wait()
    finished = utc_now()

    # ---- post processing -------------------------------------------------------------------
    tests: list[dict] = []
    output_xml = run_dir / "output.xml"
    if output_xml.is_file():
        tests = collect_results(output_xml)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "total": len(tests)}
    for tc in tests:
        counts[tc["status"]] = counts.get(tc["status"], 0) + 1

    inputs_manifest = {"dir": str(inputs_dir), "archive_mode": ns.bundle_archive, "files": []}
    inputs_manifest_file = run_dir / "inputs" / "test-inputs" / "inputs-manifest.json"
    if inputs_manifest_file.is_file():
        inputs_manifest = json.loads(inputs_manifest_file.read_text(encoding="utf-8"))
    inputs_manifest["dir"] = inputs_manifest.get("inputs_dir") or str(inputs_dir)
    inputs_manifest.setdefault("files", [])
    inputs_manifest["archived"] = ns.bundle_archive != "none" and inputs_manifest_file.is_file()
    bundle_entry = next((f for f in inputs_manifest["files"] if f["role"] == "bundle"), None)
    bundle_info = None
    bundle_text = None
    if bundle_entry:
        bundle_info = {k: bundle_entry[k] for k in ("source", "relative_path", "size", "sha256")}
        bundle_text = f"`{Path(bundle_entry['source']).name}` sha256 `{bundle_entry['sha256']}`"

    manifest = {
        "run_id": run_id,
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": finished.isoformat(timespec="seconds"),
        "robot_exit_code": rc,
        "profile": profile,
        "dry_run": ns.dry_run,
        "device": device or "",
        "command_line": [shlex.quote(a) for a in argv],
        "robot_command": shlex.join(cmd),
        "target": {"host": host or "n/a", "port": port, "user": user, "password": pw_state,
                   "key_file": get_setting("TARGET_KEY_FILE", "") or ""},
        "bundle": bundle_info,
        "inputs": inputs_manifest,
        "git": git,
        "counts": counts,
        "tests": tests,
        "controller": {
            "hostname": socket.gethostname(),
            "user": getpass.getuser(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "robotframework": md.version("robotframework"),
            "paramiko": md.version("paramiko"),
            "robotcode": md.version("robotcode"),
        },
    }
    write_result_sheet(run_dir, tests, {**manifest, "bundle": bundle_text})
    write_reproduce(run_dir, argv, manifest)
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    write_sha256sums(run_dir, recursive=True)

    print(f"\nPASS {counts['PASS']}  FAIL {counts['FAIL']}  SKIP {counts['SKIP']}  (total {counts['total']})")
    print(f"Result sheet: {run_dir / 'RESULT-SHEET.md'}")
    print(f"Log:          {run_dir / 'log.html'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
