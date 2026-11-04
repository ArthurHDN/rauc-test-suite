"""Device plugin API.

The Robot suites describe *what* RAUC has to do on every device (install to the inactive slot,
fall back after a failed boot, ...).  Everything that depends on the hardware - the bootloader,
the partition layout, how to read and reset the A/B state - is implemented by a subclass of
:class:`DevicePlugin`.  See ``DEVICES.md`` for a step by step guide to add a device and
``_template.py`` for a skeleton.

Vocabulary shared between the suites and the plugins
----------------------------------------------------
bootname        ``"A"`` or ``"B"`` (the ``bootname=`` of the slot in ``system.conf``).
slot state      ``"normal"`` (bootable) or ``"unbootable"``.
bootloader state ``{"current": "A", "active": "A", "slots": {"A": "normal", "B": "normal"}}``
                ``current``: slot the running system was booted from.
                ``active``: slot the bootloader will try first (RAUC's *primary* slot).
interface state ``"good"`` or ``"bad"`` (the RAUC term for a slot state).
"""

from __future__ import annotations

import re
import time
from typing import Callable, Protocol

BOOTNAMES = ("A", "B")

# Optional behaviours a device may not offer.  A plugin lists them in ``unsupported`` (with the
# reason) and the matching test skips itself instead of failing.
FEATURE_PLAIN_FORMAT_REJECTION = "plain-format-rejection"
FEATURE_MARK_GOOD_INACTIVE_NOOP = "mark-good-inactive-noop"
FEATURES = (FEATURE_PLAIN_FORMAT_REJECTION, FEATURE_MARK_GOOD_INACTIVE_NOOP)


class TargetAccess(Protocol):
    """What a plugin may use of the connection to the device (implemented by ``TargetSSH``)."""

    def run_on_target(self, command: str, timeout: float = 120, expected_rc: int | str | None = 0):
        """Run a shell command; returns an object with ``rc``, ``stdout`` and ``stderr``.

        Raises ``AssertionError`` when the exit code differs from *expected_rc* (``"any"`` accepts all).
        """

    def save_artifact(self, name: str, content: str) -> str:
        """Store *content* as evidence of the running test."""


def other_bootname(bootname: str) -> str:
    return {"A": "B", "B": "A"}[check_bootname(bootname)]


def check_bootname(name: str) -> str:
    if name not in BOOTNAMES:
        raise AssertionError(f"Invalid bootname '{name}', expected A or B")
    return name


def plain_format_rejected(bundle_formats: str) -> bool:
    """True if a ``bundle-formats`` value of system.conf forbids the legacy *plain* bundle format."""
    tokens = bundle_formats.split()
    if not tokens:
        return False  # RAUC default: every format is allowed
    if "-plain" in tokens:
        return True
    explicit = [t for t in tokens if not t.startswith(("-", "+"))]
    return bool(explicit) and "plain" not in explicit


class DevicePlugin:
    """Base class of a device plugin.

    A plugin class is registered automatically once the module is placed in
    ``libraries/devices/`` and the class lists its names in :attr:`devices`.
    Methods that raise ``NotImplementedError`` must be implemented; all others have a usable default.
    """

    # ---------------------------------------------------------------- declaration
    #: Device names served by the class.  A device name equals its Yocto MACHINE, which is the
    #: RAUC ``compatible`` string expected on the device (override with the MACHINE variable).
    devices: tuple[str, ...] = ()
    #: One line for ``--list-devices``.
    description: str = ""
    #: ``{feature: reason}`` for behaviours of :data:`FEATURES` the device does not provide.
    unsupported: dict[str, str] = {}
    #: Kernel messages that show filesystem / storage problems (egrep).
    kernel_error_regex: str = r"EXT4-fs error|I/O error"
    #: Kernel messages shown (informational) by the filesystem health test (egrep -i).
    storage_log_regex: str = r"ext4|i/o error"
    #: Kernel module of the network interface; empty = not checked.
    nic_module_regex: str = ""
    #: Files that an installation to the inactive slot must not modify (checksummed before/after).
    unchanged_files: tuple[str, ...] = ()
    #: Commands recorded with the target state (start and end of a run).
    state_commands: tuple[str, ...] = ()
    #: Commands recorded for every failed test.
    diagnostic_commands: tuple[str, ...] = ()

    def __init__(self, device: str, target: TargetAccess, data_mount: str = "/data", machine: str = "") -> None:
        self.device = device
        self.target = target
        self.data_mount = data_mount
        #: RAUC ``compatible`` string expected on the device (Yocto MACHINE).
        self.machine = machine or device

    # -------------------------------------------------------------------- helpers
    def run(self, command: str, timeout: float = 60, expected_rc: int | str | None = 0):
        return self.target.run_on_target(command, timeout=timeout, expected_rc=expected_rc)

    def save(self, name: str, content: str) -> None:
        self.target.save_artifact(name, content)

    @staticmethod
    def warn(message: str) -> None:
        try:
            from robot.api import logger

            logger.warn(message)
        except Exception:  # noqa: BLE001 - used outside Robot (unit tests)
            print(f"WARNING: {message}")

    @staticmethod
    def wait_until(check: Callable[[], None], timeout: float, interval: float = 5.0) -> None:
        """Call *check* until it stops raising ``AssertionError`` or *timeout* seconds passed."""
        deadline = time.monotonic() + float(timeout)
        while True:
            try:
                check()
                return
            except AssertionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(interval)

    def block_name(self, path: str) -> str:
        """Kernel name (e.g. ``mmcblk0p3``) of the block device behind *path*."""
        return self.run(f'basename "$(readlink -f {path})"').stdout.strip()

    # ---------------------------------------------------- static description (required)
    def expected_system_conf(self) -> dict[str, dict[str, str]]:
        """``{section: {key: value}}`` that ``/etc/rauc/system.conf`` must contain."""
        raise NotImplementedError

    def system_conf_placeholders(self) -> dict[str, str]:
        """Placeholders of the recipe's system.conf and their build-time value.

        Used to expand the reference ``expected/system.conf`` and to assert none is left over.
        """
        return {}

    def slot_device(self, bootname: str) -> str:
        """Block device (as written in system.conf) of the rootfs slot *bootname*."""
        for name, section in self.expected_system_conf().items():
            if name.startswith("slot.rootfs.") and section.get("bootname") == bootname:
                return section["device"]
        raise AssertionError(f"No rootfs slot with bootname {bootname} in the expected system.conf")

    def data_device(self) -> str:
        """Block device holding the persistent data partition."""
        raise NotImplementedError

    # ----------------------------------------------------------- feature switches
    def unsupported_reason(self, feature: str) -> str:
        """Empty when *feature* is supported, otherwise why it is not (the test is skipped)."""
        if feature not in FEATURES:
            raise AssertionError(f"Unknown feature '{feature}', known: {', '.join(FEATURES)}")
        if feature in self.unsupported:
            return self.unsupported[feature]
        if feature == FEATURE_PLAIN_FORMAT_REJECTION:
            from RaucHelpers import parse_ini

            conf = parse_ini(self.run("cat /etc/rauc/system.conf").stdout)
            if not plain_format_rejected(conf.get("system", {}).get("bundle-formats", "")):
                return "system.conf does not restrict bundle-formats, so the plain format is accepted"
        return ""

    # ------------------------------------------------------ checks (device specific)
    def check_installation(self) -> None:
        """The device specific integration files and tools are present (TC-1.1)."""

    def check_partition_layout(self) -> None:
        """Device specific parts of the redundant partition layout (TC-1.3), e.g. boot partitions."""

    def check_data_layout(self) -> None:
        """Device specific parts of the data partition layout (TC-1.5)."""

    def check_boot_files(self) -> None:
        """Kernel (and DTB / initrd, if applicable) are delivered by the booted rootfs (TC-2.4)."""
        raise NotImplementedError

    def check_boot_confirmation(self, bootname: str) -> None:
        """The bootloader recorded the successful boot of *bootname* (TC-5.4)."""

    def check_boot_chain_healthy(self) -> None:
        """No slot is flagged unbootable; raise ``AssertionError`` otherwise.

        Used as safety guard before every reboot, so it should be cheap.
        """
        state = self.get_bootloader_state()
        bad = [b for b in BOOTNAMES if state["slots"][b] == "unbootable"]
        if bad:
            raise AssertionError(f"Unbootable slot(s) {bad}: {state}")

    # -------------------------------------------------- A/B state (device specific)
    def get_booted_bootname(self) -> str:
        """Bootname of the slot the running system was booted from."""
        raise NotImplementedError

    def get_bootloader_state(self) -> dict:
        """Bootloader view of the A/B state; see the module documentation for the format."""
        raise NotImplementedError

    def set_primary(self, bootname: str) -> None:
        """Make the bootloader try *bootname* first, using the bootloader's own tools.

        Only used to restore the state after a test; must not raise when the state is odd.
        """
        raise NotImplementedError

    def reset_slot_status(self, bootname: str) -> None:
        """Clear a sticky 'unbootable' state of *bootname* left behind by failed boots or installs.

        Must not change which slot is primary.  No-op when nothing sticks.
        """

    # ------------------------------------------------ bootloader interface (TC-3.x)
    # The interface RAUC uses to read and write the A/B state.  The suites call it directly to
    # check it independently of the install workflow.
    def interface_get_current(self) -> str:
        raise NotImplementedError

    def interface_get_primary(self) -> str:
        raise NotImplementedError

    def interface_get_state(self, bootname: str) -> str:
        """``"good"`` or ``"bad"``."""
        raise NotImplementedError

    def interface_set_primary(self, bootname: str) -> None:
        raise NotImplementedError

    def interface_set_state(self, bootname: str, state: str) -> None:
        """*state* is ``"good"`` or ``"bad"``."""
        raise NotImplementedError

    def interface_invalid_requests(self) -> list[str]:
        """Shell commands that must fail (non-zero exit) without changing the state."""
        raise NotImplementedError

    def interface_usage_request(self) -> tuple[str, str] | None:
        """``(command, text)``: the command without arguments must fail and print *text*; None = n/a."""
        return None

    # ------------------------------------------------------------------- profile
    def profile(self) -> dict:
        """Static data for the suites (no I/O)."""
        return {
            "device": self.device,
            "machine": self.machine,
            "description": self.description,
            "kernel_error_regex": self.kernel_error_regex,
            "storage_log_regex": self.storage_log_regex,
            "nic_module_regex": self.nic_module_regex,
            "unchanged_files": list(self.unchanged_files),
            "state_commands": list(self.state_commands),
            "diagnostic_commands": list(self.diagnostic_commands),
        }


def first_match(pattern: str, text: str, what: str) -> str:
    """First group of *pattern* in *text*; ``AssertionError`` mentioning *what* otherwise."""
    m = re.search(pattern, text, re.M)
    if not m:
        raise AssertionError(f"Cannot find {what} in:\n{text}")
    return m.group(1)
