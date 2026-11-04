"""Pure-Python parsing helpers for RAUC output (no I/O to the target, no device knowledge)."""

from __future__ import annotations

import configparser
import json
import re

_BOOTNAMES = ("A", "B")


# ------------------------------------------------------------------ slot names
def other_bootname(bootname: str) -> str:
    return {"A": "B", "B": "A"}[_check_bootname(bootname)]


def bootname_to_index(bootname: str) -> int:
    return _BOOTNAMES.index(_check_bootname(bootname))


def index_to_bootname(index: int | str) -> str:
    return _BOOTNAMES[int(index)]


def bootname_to_rauc_slot(bootname: str) -> str:
    return f"rootfs.{bootname_to_index(bootname)}"


def _check_bootname(name: str) -> str:
    if name not in _BOOTNAMES:
        raise AssertionError(f"Invalid bootname '{name}', expected A or B")
    return name


# --------------------------------------------------------------------- parsing
def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """Parse a RAUC ``system.conf`` into ``{section: {key: value}}``."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # keep key case
    cp.read_string(text)
    return {section: dict(cp.items(section)) for section in cp.sections()}


def parse_rauc_status(text: str) -> dict:
    """Normalise ``rauc status --output-format=json``.

    Returns ``{compatible, booted_slot, booted_bootname, primary_slot,
    primary_bootname, slots: {name: {bootname, state, boot_status, ...}}}``.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"'rauc status' did not return JSON: {exc}\n{text[:500]}") from exc
    raw = data.get("slots", [])
    items = raw.items() if isinstance(raw, dict) else [(k, v) for entry in raw for k, v in entry.items()]
    slots = {name: dict(info) for name, info in items}
    by_boot = {s.get("bootname"): n for n, s in slots.items()}

    def to_slot(value):
        if value in slots:
            return value
        return by_boot.get(value)

    booted = to_slot(data.get("booted"))
    if booted is None:
        booted = next((n for n, s in slots.items() if s.get("state") == "booted"), None)
    primary = to_slot(data.get("boot_primary"))
    return {
        "compatible": data.get("compatible"),
        "booted_slot": booted,
        "booted_bootname": slots[booted].get("bootname") if booted else None,
        "primary_slot": primary,
        "primary_bootname": slots[primary].get("bootname") if primary else None,
        "slots": slots,
    }


def parse_rauc_info(text: str) -> dict:
    """Normalise ``rauc info`` output (JSON preferred, text as fallback).

    Returns ``{compatible, format, version, images: [{class, filename, sha256, size}]}``.
    """
    stripped = text.strip()
    start = stripped.find("{")
    if start != -1:
        try:
            return _rauc_info_from_json(json.loads(stripped[start:]))
        except json.JSONDecodeError:
            pass
    return _rauc_info_from_text(text)


def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


def _rauc_info_from_json(data: dict) -> dict:
    images = []
    for entry in _find(data, "images") or []:
        cls, info = None, entry
        if isinstance(entry, dict) and len(entry) == 1 and isinstance(next(iter(entry.values())), dict):
            cls, info = next(iter(entry.items()))
        cls = cls or info.get("slot-class") or info.get("class")
        checksum = info.get("checksum")
        sha = checksum.get("sha256") if isinstance(checksum, dict) else (checksum or info.get("sha256"))
        size = info.get("size") or (checksum.get("size") if isinstance(checksum, dict) else None)
        images.append({"class": cls, "filename": info.get("filename"), "sha256": sha,
                       "size": int(size) if size is not None else None})
    return {
        "compatible": _find(data, "compatible"),
        "format": _find(data, "format") or _find(data, "bundle-format"),
        "version": _find(data, "version"),
        "images": images,
    }


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _rauc_info_from_text(text: str) -> dict:
    text = _ANSI_RE.sub("", text)

    def grab(pattern):
        m = re.search(pattern, text, re.M | re.I)
        return m.group(1).strip().strip("'\"") if m else None

    sums = re.findall(r"Checksum:\s*([0-9a-f]{64})", text, re.I)
    # 'Size:  233.2MB (233238528 bytes)' (older RAUC: plain number)
    sizes = re.findall(r"^\s*Size:\s*(?:.*\((\d+) bytes\)|(\d+))\s*$", text, re.M)
    sizes = [a or b for a, b in sizes]
    names = re.findall(r"Filename:\s*(\S+)", text) or re.findall(r"^\s*\(\d+\)\s+(\S+)", text, re.M)
    classes = re.findall(r"Slotclass:\s*(\S+)", text) or re.findall(r"^\s*\[(\w+)\]\s*$", text, re.M)
    images = []
    for i, sha in enumerate(sums):
        images.append({
            "class": classes[i] if i < len(classes) else None,
            "filename": names[i] if i < len(names) else None,
            "sha256": sha,
            "size": int(sizes[i]) if i < len(sizes) else None,
        })
    return {
        "compatible": grab(r"^Compatible:\s*(.+)$"),
        "format": grab(r"^Bundle Format:\s*(\w+)"),
        "version": grab(r"^Version:\s*(.+)$"),
        "images": images,
    }


# ----------------------------------------------------------------------- misc
def shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(str(value))


def combined_output(result) -> str:
    """stdout and stderr of a command result joined together."""
    return f"{result.stdout}\n{result.stderr}".strip()


def last_progress_percent(text: str) -> int:
    """Last ``NN%`` found in *text* (RAUC install progress), or -1."""
    found = re.findall(r"(?<!\d)(\d{1,3})\s*%", text)
    return int(found[-1]) if found else -1


def normalise_text(text: str) -> str:
    """Strip trailing whitespace and blank lines for config comparisons."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())
