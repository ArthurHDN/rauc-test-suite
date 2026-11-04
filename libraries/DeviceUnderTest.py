"""Robot Framework library that exposes the selected device plugin as keywords.

The suites never talk to hardware specific tools; they call these keywords and the plugin of the
selected device (``libraries/devices/``) does the work.  The device is chosen with the ``DEVICE``
variable (``./run_tests.py --device <name>``) or the ``DEVICE`` setting of the environment / env file.
"""

from __future__ import annotations

from robot.api.deco import keyword
from robot.libraries.BuiltIn import BuiltIn

import audit_common as ac
import devices
from devices.base import DevicePlugin


class DeviceUnderTest:
    ROBOT_LIBRARY_SCOPE = "GLOBAL"
    ROBOT_LIBRARY_VERSION = "1.0"

    def __init__(self) -> None:
        self._plugin: DevicePlugin | None = None

    @property
    def _p(self) -> DevicePlugin:
        if self._plugin is None:
            raise AssertionError("No device selected: run 'Select Device' first (Initialise Test Run does it)")
        return self._plugin

    # ---------------------------------------------------------------- selection
    @keyword("Select Device")
    def select_device(self, device: str = "", data_mount: str = "/data", machine: str = "") -> dict:
        """Activate the plugin for *device* (default: the ``DEVICE`` setting) and return its profile."""
        name = device or ac.get_setting("DEVICE", "") or ""
        if not name:
            available = ", ".join(n for n, _ in devices.list_devices())
            raise AssertionError(
                "No device selected. Pass --device <name> to run_tests.py, or set DEVICE in the environment "
                f"or the env file. Available devices: {available}"
            )
        cls = devices.get_plugin_class(name)
        target = BuiltIn().get_library_instance("TargetSSH")
        self._plugin = cls(name, target, data_mount=data_mount, machine=machine)
        return self._plugin.profile()

    @keyword("Get Device Profile")
    def get_device_profile(self) -> dict:
        """Static data of the device: device, machine, regexes, state/diagnostic commands, ..."""
        return self._p.profile()

    @keyword("Get Unsupported Reason")
    def get_unsupported_reason(self, feature: str) -> str:
        """Empty string if the device supports *feature*, else the reason it does not."""
        return self._p.unsupported_reason(feature)

    # -------------------------------------------------------- static description
    @keyword("Get Expected System Conf")
    def get_expected_system_conf(self) -> dict:
        return self._p.expected_system_conf()

    @keyword("Get System Conf Placeholders")
    def get_system_conf_placeholders(self) -> dict:
        return self._p.system_conf_placeholders()

    @keyword("Get Slot Device")
    def get_slot_device(self, bootname: str) -> str:
        return self._p.slot_device(bootname)

    @keyword("Get Data Device")
    def get_data_device(self) -> str:
        return self._p.data_device()

    # ------------------------------------------------------------------- checks
    @keyword("Device Installation Should Be Complete")
    def device_installation_should_be_complete(self) -> None:
        self._p.check_installation()

    @keyword("Device Partition Layout Should Be Valid")
    def device_partition_layout_should_be_valid(self) -> None:
        self._p.check_partition_layout()

    @keyword("Device Data Layout Should Be Valid")
    def device_data_layout_should_be_valid(self) -> None:
        self._p.check_data_layout()

    @keyword("Boot Files Should Match Running Kernel")
    def boot_files_should_match_running_kernel(self) -> None:
        self._p.check_boot_files()

    @keyword("Boot Confirmation Should Be Recorded")
    def boot_confirmation_should_be_recorded(self, bootname: str) -> None:
        self._p.check_boot_confirmation(bootname)

    @keyword("Boot Chain Should Be Healthy")
    def boot_chain_should_be_healthy(self) -> None:
        self._p.check_boot_chain_healthy()

    # ---------------------------------------------------------------- A/B state
    @keyword("Get Booted Bootname")
    def get_booted_bootname(self) -> str:
        return self._p.get_booted_bootname()

    @keyword("Get Bootloader State")
    def get_bootloader_state(self) -> dict:
        """``{current, active, slots: {A, B}}`` as seen by the bootloader."""
        return self._p.get_bootloader_state()

    @keyword("Set Bootloader Primary")
    def set_bootloader_primary(self, bootname: str) -> None:
        self._p.set_primary(bootname)

    @keyword("Reset Slot Boot Status")
    def reset_slot_boot_status(self, bootname: str) -> None:
        self._p.reset_slot_status(bootname)

    # ------------------------------------------------------ bootloader interface
    @keyword("Bootloader Interface Get Current")
    def interface_get_current(self) -> str:
        return self._p.interface_get_current()

    @keyword("Bootloader Interface Get Primary")
    def interface_get_primary(self) -> str:
        return self._p.interface_get_primary()

    @keyword("Bootloader Interface Get State")
    def interface_get_state(self, bootname: str) -> str:
        return self._p.interface_get_state(bootname)

    @keyword("Bootloader Interface Set Primary")
    def interface_set_primary(self, bootname: str) -> None:
        self._p.interface_set_primary(bootname)

    @keyword("Bootloader Interface Set State")
    def interface_set_state(self, bootname: str, state: str) -> None:
        self._p.interface_set_state(bootname, state)

    @keyword("Get Bootloader Interface Invalid Requests")
    def get_interface_invalid_requests(self) -> list:
        return self._p.interface_invalid_requests()

    @keyword("Get Bootloader Interface Usage Request")
    def get_interface_usage_request(self) -> list:
        """``[command, text]`` or an empty list when not applicable."""
        usage = self._p.interface_usage_request()
        return list(usage) if usage else []
