"""Raspberry Pi 4 (64 bit) with U-Boot and meta-rauc-community/meta-rauc-raspberrypi.

Boot chain: RPi firmware -> U-Boot -> ``boot.scr`` (A/B logic) -> kernel from the booted rootfs.

* RAUC uses its built-in ``uboot`` bootloader backend, i.e. ``fw_printenv`` / ``fw_setenv``
  on the U-Boot environment file ``/boot/uboot.env`` (FAT partition 1).
* State lives in the U-Boot variables ``BOOT_ORDER`` ("A B") and ``BOOT_A_LEFT`` / ``BOOT_B_LEFT``
  (remaining boot attempts, hexadecimal, 3 after a good boot).  A slot is bootable when it is part of
  ``BOOT_ORDER`` and has attempts left; ``rauc status mark-bad`` removes it from the order.
* The MBR partition table has no partition labels: slots are ``/dev/mmcblk0p2`` (A) and
  ``/dev/mmcblk0p3`` (B), data is ``/dev/mmcblk0p5``.  The kernel command line carries ``rauc.slot=A|B``.
* The kernel image is loaded by U-Boot from ``/boot/Image`` *inside the slot's rootfs*; the
  device tree comes from the firmware (boot partition) and is not part of an update.
* ``rauc-mark-good.service`` restores the attempt counter after a successful boot.
"""

from __future__ import annotations

import re
import shlex
import time

from .base import BOOTNAMES, FEATURE_MARK_GOOD_INACTIVE_NOOP, DevicePlugin, check_bootname

SLOT_DEVICES = {"A": "/dev/mmcblk0p2", "B": "/dev/mmcblk0p3"}
BOOT_DEVICE = "/dev/mmcblk0p1"
DATA_DEVICE = "/dev/mmcblk0p5"
BOOT_ATTEMPTS = 3
TMP = "/tmp"


def _hex(value: str | None) -> int:
    """U-Boot (and RAUC) treat the attempt counters as hexadecimal numbers."""
    try:
        return int(value or "0", 16)
    except ValueError:
        return 0


def state_from_env(env: dict[str, str], current: str) -> dict:
    """Bootloader state (see ``devices.base``) from the BOOT_* variables, as RAUC's U-Boot backend reads it."""
    order = [b for b in env.get("BOOT_ORDER", "").split() if b in BOOTNAMES]
    left = {b: _hex(env.get(f"BOOT_{b}_LEFT")) for b in BOOTNAMES}
    good = {b: b in order and left[b] > 0 for b in BOOTNAMES}
    active = next((b for b in order if good[b]), "")
    return {
        "current": current,
        "active": active,
        "slots": {b: "normal" if good[b] else "unbootable" for b in BOOTNAMES},
    }


class RaspberryPi4(DevicePlugin):
    devices = ("raspberrypi4-64",)
    description = "Raspberry Pi 4 (64 bit), SD card, U-Boot + RAUC uboot backend (meta-rauc-community)"
    unsupported = {
        FEATURE_MARK_GOOD_INACTIVE_NOOP: (
            "the U-Boot backend restores the attempt counter of any slot on 'mark-good'; "
            "there is no running-slot restriction like on the Jetson backend"
        ),
    }
    kernel_error_regex = r"EXT4-fs error|I/O error|mmcblk[0-9]+.*(timeout|error)"
    storage_log_regex = r"ext4|i/o error|mmcblk"
    state_commands = (
        "cat /proc/device-tree/model",
        "fw_printenv",
        "cat /etc/fw_env.config",
        "lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT",
        "ls -l /boot",
    )
    diagnostic_commands = ("fw_printenv BOOT_ORDER BOOT_A_LEFT BOOT_B_LEFT", "cat /proc/cmdline")

    # --------------------------------------------------------------- description
    def expected_system_conf(self) -> dict[str, dict[str, str]]:
        return {
            "system": {
                "compatible": self.machine,
                "bootloader": "uboot",
                "data-directory": f"{self.data_mount}/",
            },
            "keyring": {"path": "/etc/rauc/ca.cert.pem"},
            "slot.rootfs.0": {"device": SLOT_DEVICES["A"], "bootname": "A", "type": "ext4"},
            "slot.rootfs.1": {"device": SLOT_DEVICES["B"], "bootname": "B", "type": "ext4"},
        }

    def system_conf_placeholders(self) -> dict[str, str]:
        return {"@@MACHINE@@": self.machine}

    def data_device(self) -> str:
        return DATA_DEVICE

    # -------------------------------------------------------------------- checks
    def check_installation(self) -> None:
        self.run("command -v fw_printenv")
        self.run("command -v fw_setenv")
        self.run("test -s /etc/fw_env.config")
        # The environment is created by boot.scr on the first boot.
        self.run("test -f /boot/uboot.env")
        self.run("fw_printenv BOOT_ORDER")
        cmdline = self.run("cat /proc/cmdline").stdout
        if not re.search(r"\brauc\.slot=[AB]\b", cmdline):
            raise AssertionError(f"Kernel command line has no rauc.slot= (not booted through boot.scr?): {cmdline}")

    def check_partition_layout(self) -> None:
        boot = self.run("awk '$2==\"/boot\" {print $1, $3}' /proc/mounts").stdout.strip()
        if boot != f"{BOOT_DEVICE} vfat":
            raise AssertionError(f"/boot must be the FAT partition {BOOT_DEVICE}, found: {boot or 'not mounted'}")

    def check_boot_files(self) -> None:
        """U-Boot loads /boot/Image of the booted rootfs.

        The FAT boot partition is mounted on /boot and hides the rootfs copy, so the root filesystem
        is bind mounted (non-recursively) to look at it.  The DTB is provided by the firmware.
        """
        uname = self.run("uname -r").stdout.strip()
        mnt = f"{TMP}/rauc-test-rootfs"
        self.run(f"mkdir -p {mnt} && mount --bind / {mnt}")
        try:
            self.run(f'test -s {mnt}/boot/Image && grep -c -- "{uname}" {mnt}/boot/Image', timeout=120)
        finally:
            self.run(f"umount {mnt}; rmdir {mnt}", expected_rc="any")
        self.run(f"test -d /lib/modules/{uname}")

    def check_boot_confirmation(self, bootname: str) -> None:
        """rauc-mark-good.service restores the attempt counter (U-Boot decrements it on every boot)."""

        def restored() -> None:
            env = self._env()
            left = _hex(env.get(f"BOOT_{check_bootname(bootname)}_LEFT"))
            if left != BOOT_ATTEMPTS:
                raise AssertionError(
                    f"BOOT_{bootname}_LEFT is {left}, expected {BOOT_ATTEMPTS} after a confirmed boot "
                    "(did rauc-mark-good.service run?)"
                )

        self.wait_until(restored, timeout=180, interval=5)
        self.check_boot_chain_healthy()

    def check_boot_chain_healthy(self) -> None:
        """A bootable slot must be left (boot.scr skips slots without attempts, so a single bad slot is fine)."""
        env = self._env()
        self.save(
            f"uboot-env-{time.strftime('%H%M%S')}.txt",
            "\n".join(f"{k}={v}" for k, v in sorted(env.items()) if k.startswith("BOOT_")),
        )
        state = state_from_env(env, self.get_booted_bootname())
        if not state["active"]:
            raise AssertionError(f"No bootable slot left in the U-Boot environment: {state}")

    # -------------------------------------------------------------------- state
    def _env(self) -> dict[str, str]:
        out = self.run("fw_printenv", timeout=60).stdout
        env = {}
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                env[key] = value
        return env

    def _setenv(self, name: str, value: str) -> None:
        self.run(f"fw_setenv {name} {shlex.quote(value)}", timeout=60)

    def get_booted_bootname(self) -> str:
        cmdline = self.run("cat /proc/cmdline").stdout
        m = re.search(r"\brauc\.slot=([AB])\b", cmdline)
        if not m:
            raise AssertionError(f"No rauc.slot= on the kernel command line: {cmdline}")
        return m.group(1)

    def get_bootloader_state(self) -> dict:
        return state_from_env(self._env(), self.get_booted_bootname())

    def set_primary(self, bootname: str) -> None:
        """Same effect as RAUC's backend: slot first in BOOT_ORDER with a full attempt counter."""
        check_bootname(bootname)
        order = [b for b in self._env().get("BOOT_ORDER", "").split() if b in BOOTNAMES and b != bootname]
        self._setenv(f"BOOT_{bootname}_LEFT", f"{BOOT_ATTEMPTS:x}")
        self._setenv("BOOT_ORDER", " ".join([bootname] + order))

    def reset_slot_status(self, bootname: str) -> None:
        """Re-add a slot that RAUC marked bad (removed from BOOT_ORDER, attempts 0) without changing the primary."""
        check_bootname(bootname)
        env = self._env()
        order = [b for b in env.get("BOOT_ORDER", "").split() if b in BOOTNAMES]
        if bootname in order and _hex(env.get(f"BOOT_{bootname}_LEFT")) > 0:
            return
        self.warn(f"Resetting boot state of slot {bootname}")
        if bootname not in order:
            order.append(bootname)
        self._setenv(f"BOOT_{bootname}_LEFT", f"{BOOT_ATTEMPTS:x}")
        self._setenv("BOOT_ORDER", " ".join(order))
        status = self.get_bootloader_state()["slots"][bootname]
        if status != "normal":
            raise AssertionError(f"Boot state of slot {bootname} could not be reset (now {status})")

    # ------------------------------------------------------ bootloader interface
    # RAUC's own U-Boot backend is exercised through the 'rauc status' CLI.
    def _rauc_status(self) -> dict:
        from RaucHelpers import parse_rauc_status

        return parse_rauc_status(self.run("rauc status --output-format=json", timeout=60).stdout)

    @staticmethod
    def _slot_name(bootname: str) -> str:
        return f"rootfs.{BOOTNAMES.index(check_bootname(bootname))}"

    def interface_get_current(self) -> str:
        return self._rauc_status()["booted_bootname"] or ""

    def interface_get_primary(self) -> str:
        return self._rauc_status()["primary_bootname"] or ""

    def interface_get_state(self, bootname: str) -> str:
        return self._rauc_status()["slots"][self._slot_name(bootname)]["boot_status"]

    def interface_set_primary(self, bootname: str) -> None:
        self.run(f"rauc status mark-active {self._slot_name(bootname)}")

    def interface_set_state(self, bootname: str, state: str) -> None:
        self.run(f"rauc status mark-{state} {self._slot_name(bootname)}")

    def interface_invalid_requests(self) -> list[str]:
        return [
            "rauc status mark-good rootfs.9",
            "rauc status mark-active rootfs.9",
            "rauc status mark-bad nonexistent",
            "rauc status mark-maybe rootfs.0",
        ]

    # interface_usage_request: the RAUC CLI is not part of the device integration -> not applicable
