"""Skeleton of a device plugin - copy to ``libraries/devices/<my_device>.py`` and fill in the blanks.

This module is never loaded (names starting with ``_`` are skipped).  See DEVICES.md for the
complete guide.  Only what differs from the defaults of ``DevicePlugin`` has to be implemented.
"""

from __future__ import annotations

from .base import BOOTNAMES, DevicePlugin, check_bootname  # noqa: F401


class MyDevice(DevicePlugin):
    # Names under which the device is selected (DEVICE=... / --device).  Use the Yocto MACHINE name:
    # it is also the RAUC "compatible" string expected on the device.
    devices = ()  # e.g. ("my-board",)
    description = "One line shown by ./run_tests.py --list-devices"

    # Optional: features this device does not provide ({feature: reason}), see base.FEATURES.
    unsupported = {}
    # Optional: kernel messages that indicate storage problems, NIC module, checked files, evidence commands.
    kernel_error_regex = r"EXT4-fs error|I/O error"
    nic_module_regex = ""
    unchanged_files = ()
    state_commands = ()
    diagnostic_commands = ()

    # ---- what system.conf and the partitions must look like (TC-1.2, TC-1.3, TC-1.5) ----
    def expected_system_conf(self):
        return {
            "system": {"compatible": self.machine, "bootloader": "<uboot|grub|efi|barebox|custom>"},
            "slot.rootfs.0": {"device": "<A device>", "bootname": "A", "type": "ext4"},
            "slot.rootfs.1": {"device": "<B device>", "bootname": "B", "type": "ext4"},
        }

    def data_device(self):
        return "<device of the persistent data partition>"

    # ---- A/B state of the bootloader (TC-3.x, 5.x, 6.x, 7.x) ----
    def get_booted_bootname(self):
        raise NotImplementedError  # "A" or "B"

    def get_bootloader_state(self):
        raise NotImplementedError  # {"current": "A", "active": "A", "slots": {"A": "normal", "B": "normal"}}

    def set_primary(self, bootname):
        raise NotImplementedError  # make the bootloader try `bootname` first

    # ---- boot files (TC-2.4) and the interface RAUC uses for the bootloader (TC-3.x) ----
    def check_boot_files(self):
        raise NotImplementedError

    def interface_get_current(self):
        raise NotImplementedError

    def interface_get_primary(self):
        raise NotImplementedError

    def interface_get_state(self, bootname):
        raise NotImplementedError  # "good" or "bad"

    def interface_set_primary(self, bootname):
        raise NotImplementedError

    def interface_set_state(self, bootname, state):
        raise NotImplementedError

    def interface_invalid_requests(self):
        raise NotImplementedError  # commands that must fail without changing the state
