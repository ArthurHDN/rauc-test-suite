"""Locate, verify and archive the externally supplied test inputs.

The tests never build or generate anything on the machine that runs them.  Everything they
need comes from one *inputs directory* (``--inputs`` of run_tests.py / ``-v INPUTS_DIR:...``)::

    <inputs>/
      bundle/<name>.raucb          bundle under test (or pass --bundle FILE)
      expected/system.conf         reference /etc/rauc/system.conf (@RAUC_COMPATIBLE@ placeholder allowed)
      expected/ca.cert.pem         reference keyring certificate
      expected/rootfs.sha256       sha256 of the rootfs image that must be inside the bundle
      negative/wrong-signature.raucb    signed with a certificate that is not in the keyring
      negative/wrong-compatible.raucb   signed correctly, compatible string of another machine
      negative/plain-format.raucb       legacy "plain" bundle format
      negative/broken-rootfs.raucb      valid bundle whose rootfs cannot boot
      inputs.json                  optional metadata incl. {"files": {relative-path: sha256}}

Every file is optional; tests that need a missing one are skipped (or fail with a clear message
for the bundle).  ``tools/prepare_qa_inputs.py`` of the source repository creates such a directory.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from audit_common import sha256_file

__all__ = ["discover_inputs", "verify_inputs", "archive_inputs", "NEGATIVE_BUNDLES"]

NEGATIVE_BUNDLES = ("wrong-signature", "wrong-compatible", "plain-format", "broken-rootfs")


def _newest(paths: list[Path]) -> Path | None:
    files = [p for p in paths if p.is_file()]
    real = [p for p in files if not p.is_symlink()]
    pool = real or files
    return max(pool, key=lambda p: p.stat().st_mtime) if pool else None


def discover_inputs(inputs_dir: str = "", bundle_path: str = "") -> dict:
    """Resolve all input files.  Missing items are returned as empty strings."""
    d = Path(inputs_dir).expanduser() if inputs_dir else None
    exists = bool(d and d.is_dir())
    base = d.resolve() if exists else None

    def pick(rel: str) -> str:
        p = base / rel if base else None
        return str(p) if p and p.is_file() else ""

    if bundle_path:
        bundle = Path(bundle_path).expanduser()
        bundle_s = str(bundle.resolve()) if bundle.is_file() else ""
    elif base:
        newest = _newest(list((base / "bundle").glob("*.raucb")) + list(base.glob("*.raucb")))
        bundle_s = str(newest) if newest else ""
    else:
        bundle_s = ""

    rootfs_sha = ""
    rootfs_name = ""
    sha_file = pick("expected/rootfs.sha256")
    if sha_file:
        fields = Path(sha_file).read_text(encoding="utf-8").split()
        if fields:
            rootfs_sha = fields[0].lower()
            rootfs_name = fields[1].lstrip("*") if len(fields) > 1 else ""

    metadata: dict = {}
    meta_file = pick("inputs.json")
    if meta_file:
        try:
            metadata = json.loads(Path(meta_file).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{meta_file} is not valid JSON: {exc}") from exc

    return {
        "dir": str(base) if base else "",
        "dir_requested": str(inputs_dir),
        "bundle": bundle_s,
        "system_conf": pick("expected/system.conf"),
        "ca_cert": pick("expected/ca.cert.pem"),
        "rootfs_sha256_file": sha_file,
        "rootfs_sha256": rootfs_sha,
        "rootfs_name": rootfs_name,
        "negative": {n: p for n in NEGATIVE_BUNDLES if (p := pick(f"negative/{n}.raucb"))},
        "metadata_file": meta_file,
        "metadata": metadata,
    }


def verify_inputs(inputs: dict) -> list:
    """Compare files against ``inputs.json`` -> ``files``; returns a list of problems (empty = OK)."""
    problems: list[str] = []
    base = inputs.get("dir")
    files = (inputs.get("metadata") or {}).get("files") or {}
    if not base:
        return ["inputs directory not found: " + str(inputs.get("dir_requested") or "(not set)")]
    for rel, expected in sorted(files.items()):
        p = Path(base) / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
        elif sha256_file(p) != expected:
            problems.append(f"checksum mismatch: {rel}")
    explicit = inputs.get("bundle")
    if explicit and files and not any(str(Path(base) / rel) == explicit for rel in files):
        problems.append(f"bundle {explicit} is not listed in inputs.json (supplied separately)")
    return problems


def _place(src: Path, dest: Path, mode: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    if mode == "link":
        try:
            os.link(src, dest)
            return dest
        except OSError:
            pass
    shutil.copy2(src, dest)
    return dest


def archive_inputs(inputs: dict, dest_dir: str, mode: str = "link") -> dict:
    """Preserve all used input files under *dest_dir* (same layout as an inputs directory).

    ``*.raucb`` are hard-linked (``link``, falls back to copy), copied (``copy``) or only hashed
    (``none``); small files are always copied.  Writes ``inputs-manifest.json``.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    items: list[tuple[str, str, str]] = []  # (role, source, relative destination)
    if inputs.get("bundle"):
        items.append(("bundle", inputs["bundle"], f"bundle/{Path(inputs['bundle']).name}"))
    for role, key, rel in (("system_conf", "system_conf", "expected/system.conf"),
                           ("ca_cert", "ca_cert", "expected/ca.cert.pem"),
                           ("rootfs_sha256", "rootfs_sha256_file", "expected/rootfs.sha256"),
                           ("metadata", "metadata_file", "inputs.json")):
        if inputs.get(key):
            items.append((role, inputs[key], rel))
    for name, path in (inputs.get("negative") or {}).items():
        items.append((f"negative:{name}", path, f"negative/{name}.raucb"))

    entries = []
    for role, source, rel in items:
        src = Path(source)
        big = src.suffix == ".raucb"
        use_mode = mode if big else "copy"
        archived = "" if use_mode == "none" else str(_place(src, dest / rel, use_mode))
        entries.append({
            "role": role, "source": str(src), "archived": archived, "relative_path": rel,
            "mode": use_mode, "size": src.stat().st_size, "sha256": sha256_file(src),
        })
    manifest = {"inputs_dir": inputs.get("dir"), "archive_mode": mode, "files": entries}
    (dest / "inputs-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
