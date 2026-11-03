"""Shared helpers: settings lookup, artifact directory layout, hashing.

Layout of a run directory (``${OUTPUT DIR}``)::

    output.xml log.html report.html debug.log console.log   Robot Framework results
    manifest.json RESULT-SHEET.md SHA256SUMS reproduce.sh   written by run_tests.py
    inputs/                                                 files used for the run
    audit/commands.jsonl                                    every command, whole run
    audit/results.jsonl                                     one line per finished test
    artifacts/<TC-id>_<slug>/                               per-test evidence
        commands.log  test.json  SHA256SUMS  <files>
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

RAUC_TESTS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = RAUC_TESTS_DIR / ".env"


def parse_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` file.

    Supports ``export`` prefixes, ``#`` comment lines and single/double quoted
    values.  Inline comments are deliberately NOT stripped so passwords may
    contain ``#``.  An empty value (``KEY=``) is a valid, explicit empty value.
    """
    values: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return values
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def env_file_path() -> Path:
    return Path(os.environ.get("JETSON_ENV_FILE") or DEFAULT_ENV_FILE)


def get_setting(name: str, default: str | None = None) -> str | None:
    """Look up a setting: process environment first, then the env file.

    A variable that is present but empty is returned as ``""`` (needed for
    empty passwords); only a missing variable yields *default*.
    """
    if name in os.environ:
        return os.environ[name]
    file_values = parse_env_file(env_file_path())
    if name in file_values:
        return file_values[name]
    return default


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len].strip("-") or "unnamed"


def test_dir_name(test_name: str) -> str:
    """``TC-1.1 RAUC Packages Installed`` -> ``TC-1.1_rauc-packages-installed``."""
    m = re.match(r"(TC-\d+\.\d+)\s*(.*)", test_name)
    if m:
        rest = slugify(m.group(2))
        return f"{m.group(1)}_{rest}" if m.group(2) else m.group(1)
    return slugify(test_name)


def output_dir() -> Path:
    from robot.libraries.BuiltIn import BuiltIn

    return Path(BuiltIn().get_variable_value("${OUTPUT DIR}"))


def current_artifact_dir(create: bool = True) -> Path:
    """Artifact directory of the running test (or suite for setup/teardown)."""
    from robot.libraries.BuiltIn import BuiltIn

    bi = BuiltIn()
    out = Path(bi.get_variable_value("${OUTPUT DIR}"))
    test = bi.get_variable_value("${TEST NAME}")
    if test:
        d = out / "artifacts" / test_dir_name(test)
    else:
        suite = bi.get_variable_value("${SUITE NAME}") or "run"
        d = out / "artifacts" / f"_suite_{slugify(suite)}"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def sha256_file(path: str | os.PathLike, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def write_sha256sums(directory: Path, name: str = "SHA256SUMS", recursive: bool = False) -> None:
    """Write a ``sha256sum -c`` compatible manifest for *directory*."""
    files = directory.rglob("*") if recursive else directory.iterdir()
    lines = []
    for f in sorted(files):
        if f.is_file() and not f.is_symlink() and f.name != name:
            lines.append(f"{sha256_file(f)}  {f.relative_to(directory).as_posix()}")
    (directory / name).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
