"""Test doubles shared by the unit tests (no device, no SSH needed)."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

LIBRARIES = Path(__file__).resolve().parent.parent / "libraries"
if str(LIBRARIES) not in sys.path:
    sys.path.insert(0, str(LIBRARIES))


class FakeTarget:
    """Implements ``devices.base.TargetAccess`` with canned answers.

    *handlers* is a list of ``(regex, callable(match) -> stdout | (rc, stdout))`` tried in order against the
    command; an unknown command fails the test so plugins cannot silently run unexpected things.
    """

    def __init__(self, handlers=()):
        self.handlers = list(handlers)
        self.commands: list[str] = []
        self.artifacts: dict[str, str] = {}

    def run_on_target(self, command, timeout=120, expected_rc=0):
        self.commands.append(command)
        for pattern, handler in self.handlers:
            match = re.search(pattern, command)
            if match:
                result = handler(match) if callable(handler) else handler
                rc, out = result if isinstance(result, tuple) else (0, result)
                break
        else:
            raise AssertionError(f"FakeTarget: unexpected command: {command}")
        if expected_rc != "any" and rc != expected_rc:
            raise AssertionError(f"'{command}' returned {rc}, expected {expected_rc}")
        return SimpleNamespace(rc=rc, stdout=out, stderr="")

    def save_artifact(self, name, content):
        self.artifacts[name] = content
        return name


class FakeUBootTarget(FakeTarget):
    """A target running RAUC's U-Boot integration: stateful ``fw_printenv`` / ``fw_setenv``."""

    def __init__(self, booted="A", order="A B", left_a="3", left_b="3"):
        self.env = {"BOOT_ORDER": order, "BOOT_A_LEFT": left_a, "BOOT_B_LEFT": left_b, "other": "x"}
        self.booted = booted
        super().__init__(
            [
                (r"^fw_printenv$", lambda m: "\n".join(f"{k}={v}" for k, v in self.env.items())),
                (r"^fw_setenv (\S+) (.*)$", self._setenv),
                (r"^cat /proc/cmdline$", lambda m: f"console=ttyS0 root=/dev/mmcblk0p2 rauc.slot={self.booted} rw"),
            ]
        )

    def _setenv(self, match):
        self.env[match.group(1)] = " ".join(shlex.split(match.group(2)))
        return ""
