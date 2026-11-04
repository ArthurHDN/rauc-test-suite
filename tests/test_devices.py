"""Unit tests of the device plugin layer.  Run: ``.venv/bin/python -m unittest discover -s tests``."""

from __future__ import annotations

import unittest

from fakes import FakeTarget, FakeUBootTarget  # noqa: F401 - also puts libraries/ on sys.path

from devices import get_plugin_class, list_devices, registry
from devices.base import BOOTNAMES, FEATURES, DevicePlugin, other_bootname, plain_format_rejected
from devices.jetson_orin_nano import parse_nvbootctrl_slots
from devices.raspberrypi import state_from_env

DEVICES = ("jetson-orin-nano-devkit", "jetson-orin-nano-devkit-nvme", "raspberrypi4-64")

NVBOOTCTRL_BOOTLOADER = """Current bootloader slot: A
Active bootloader slot: A
num_slots: 2
slot: 0,             status: normal
slot: 1,             status: normal
"""
NVBOOTCTRL_ROOTFS_B_BAD = """Current rootfs slot: A
Active rootfs slot: A
num_slots: 2
slot: 0,             retry_count: 3, status: normal
slot: 1,             retry_count: 0, status: unbootable
"""


class RegistryTest(unittest.TestCase):
    def test_all_devices_are_discovered(self):
        self.assertEqual(set(DEVICES), set(registry()))

    def test_listing_is_sorted_and_described(self):
        listing = list_devices()
        self.assertEqual(sorted(n for n, _ in listing), [n for n, _ in listing])
        self.assertTrue(all(description for _, description in listing))

    def test_unknown_device_is_rejected_with_the_choices(self):
        with self.assertRaises(AssertionError) as ctx:
            get_plugin_class("does-not-exist")
        for name in DEVICES:
            self.assertIn(name, str(ctx.exception))

    def test_template_is_not_registered(self):
        self.assertFalse(any("template" in name for name in registry()))


class ContractTest(unittest.TestCase):
    """What every plugin has to provide, without talking to a device."""

    def plugin(self, device):
        return get_plugin_class(device)(device, FakeTarget())

    def test_static_description(self):
        for device in DEVICES:
            with self.subTest(device=device):
                plugin = self.plugin(device)
                conf = plugin.expected_system_conf()
                self.assertEqual(device, conf["system"]["compatible"])
                bootnames = sorted(s["bootname"] for n, s in conf.items() if n.startswith("slot.rootfs."))
                self.assertEqual(list(BOOTNAMES), bootnames)
                for bootname in BOOTNAMES:
                    self.assertTrue(plugin.slot_device(bootname).startswith("/dev/"))
                self.assertTrue(plugin.data_device().startswith("/dev/"))
                self.assertIsInstance(plugin.system_conf_placeholders(), dict)

    def test_profile_has_the_keys_used_by_the_suites(self):
        keys = {
            "device", "machine", "description", "kernel_error_regex", "storage_log_regex",
            "nic_module_regex", "unchanged_files", "state_commands", "diagnostic_commands",
        }
        for device in DEVICES:
            with self.subTest(device=device):
                self.assertLessEqual(keys, set(self.plugin(device).profile()))

    def test_interface_description(self):
        for device in DEVICES:
            with self.subTest(device=device):
                plugin = self.plugin(device)
                self.assertTrue(plugin.interface_invalid_requests())
                usage = plugin.interface_usage_request()
                self.assertTrue(usage is None or len(usage) == 2)

    def test_machine_can_be_overridden(self):
        plugin = get_plugin_class("raspberrypi4-64")("raspberrypi4-64", FakeTarget(), machine="my-machine")
        self.assertEqual("my-machine", plugin.expected_system_conf()["system"]["compatible"])

    def test_unsupported_features_are_known(self):
        for cls in set(registry().values()):
            self.assertLessEqual(set(cls.unsupported), set(FEATURES))


class HelperTest(unittest.TestCase):
    def test_other_bootname(self):
        self.assertEqual("B", other_bootname("A"))
        self.assertEqual("A", other_bootname("B"))
        with self.assertRaises(AssertionError):
            other_bootname("C")

    def test_plain_format_rejected(self):
        cases = {
            "": False,
            "-plain": True,
            "plain verity": False,
            "verity": True,
            "-plain +verity": True,
            "+verity": False,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, plain_format_rejected(value))


class NvbootctrlParserTest(unittest.TestCase):
    def test_bootloader_chain(self):
        self.assertEqual(
            {"current": "A", "active": "A", "slots": {"A": "normal", "B": "normal"}},
            parse_nvbootctrl_slots(NVBOOTCTRL_BOOTLOADER),
        )

    def test_rootfs_chain_with_retry_count(self):
        info = parse_nvbootctrl_slots(NVBOOTCTRL_ROOTFS_B_BAD)
        self.assertEqual({"A": "normal", "B": "unbootable"}, info["slots"])

    def test_garbage_is_an_error(self):
        with self.assertRaises(AssertionError):
            parse_nvbootctrl_slots("nope")


class StateFromEnvTest(unittest.TestCase):
    def state(self, order="A B", a="3", b="3", current="A"):
        return state_from_env({"BOOT_ORDER": order, "BOOT_A_LEFT": a, "BOOT_B_LEFT": b}, current)

    def test_healthy(self):
        self.assertEqual({"current": "A", "active": "A", "slots": {"A": "normal", "B": "normal"}}, self.state())

    def test_primary_is_first_slot_in_order_with_attempts(self):
        self.assertEqual("B", self.state(order="B A")["active"])
        self.assertEqual("B", self.state(order="A B", a="0")["active"])

    def test_slot_missing_in_order_is_unbootable(self):
        state = self.state(order="B")
        self.assertEqual("unbootable", state["slots"]["A"])
        self.assertEqual("B", state["active"])

    def test_attempt_counters_are_hexadecimal(self):
        self.assertEqual("normal", self.state(a="a")["slots"]["A"])
        self.assertEqual("unbootable", self.state(a="zz")["slots"]["A"])

    def test_no_bootable_slot(self):
        self.assertEqual("", self.state(order="", a="0", b="0")["active"])


class JetsonPluginTest(unittest.TestCase):
    def plugin(self, device="jetson-orin-nano-devkit-nvme", rootfs=NVBOOTCTRL_ROOTFS_B_BAD):
        target = FakeTarget(
            [
                (r"nvbootctrl -t bootloader dump-slots-info", NVBOOTCTRL_BOOTLOADER),
                (r"nvbootctrl -t rootfs dump-slots-info", rootfs),
                (r"nvbootctrl get-current-slot", "1\n"),
            ]
        )
        return get_plugin_class(device)(device, target), target

    def test_rootfs_chain_flag_is_merged_into_the_state(self):
        plugin, _ = self.plugin()
        self.assertEqual({"A": "normal", "B": "unbootable"}, plugin.get_bootloader_state()["slots"])

    def test_booted_bootname(self):
        plugin, _ = self.plugin()
        self.assertEqual("B", plugin.get_booted_bootname())

    def test_unhealthy_chain_is_detected_and_saved(self):
        plugin, target = self.plugin()
        with self.assertRaises(AssertionError):
            plugin.check_boot_chain_healthy()
        self.assertEqual(2, len(target.artifacts))

    def test_variants_differ_in_storage_patterns_only(self):
        nvme = get_plugin_class("jetson-orin-nano-devkit-nvme")
        sd = get_plugin_class("jetson-orin-nano-devkit")
        self.assertIn("nvme", nvme.kernel_error_regex)
        self.assertIn("mmcblk", sd.kernel_error_regex)
        self.assertEqual(nvme.expected_system_conf, sd.expected_system_conf)

    def test_plain_format_rejection_is_taken_from_the_system_conf(self):
        plugin, target = self.plugin()
        target.handlers.append((r"cat /etc/rauc/system.conf", "[system]\nbundle-formats=-plain\n"))
        self.assertEqual("", plugin.unsupported_reason("plain-format-rejection"))
        target.handlers.insert(0, (r"cat /etc/rauc/system.conf", "[system]\ncompatible=x\n"))
        self.assertNotEqual("", plugin.unsupported_reason("plain-format-rejection"))

    def test_unknown_feature_is_an_error(self):
        plugin, _ = self.plugin()
        with self.assertRaises(AssertionError):
            plugin.unsupported_reason("no-such-feature")


class RaspberryPiPluginTest(unittest.TestCase):
    DEVICE = "raspberrypi4-64"

    def plugin(self, **kwargs):
        target = FakeUBootTarget(**kwargs)
        return get_plugin_class(self.DEVICE)(self.DEVICE, target), target

    def test_booted_bootname_comes_from_the_kernel_command_line(self):
        plugin, _ = self.plugin(booted="B")
        self.assertEqual("B", plugin.get_booted_bootname())

    def test_state(self):
        plugin, _ = self.plugin(booted="B", order="B A")
        self.assertEqual(
            {"current": "B", "active": "B", "slots": {"A": "normal", "B": "normal"}}, plugin.get_bootloader_state()
        )

    def test_set_primary_puts_the_slot_first_with_full_attempts(self):
        plugin, target = self.plugin(order="A B", left_b="1")
        plugin.set_primary("B")
        self.assertEqual("B A", target.env["BOOT_ORDER"])
        self.assertEqual("3", target.env["BOOT_B_LEFT"])
        self.assertEqual("B", plugin.get_bootloader_state()["active"])

    def test_set_primary_re_adds_a_slot_removed_from_the_order(self):
        plugin, target = self.plugin(order="A", left_b="0")
        plugin.set_primary("B")
        self.assertEqual("B A", target.env["BOOT_ORDER"])

    def test_reset_slot_status_heals_a_bad_slot_but_keeps_the_primary(self):
        plugin, target = self.plugin(order="A", left_b="0")
        plugin.reset_slot_status("B")
        self.assertEqual("A B", target.env["BOOT_ORDER"])
        self.assertEqual("3", target.env["BOOT_B_LEFT"])
        self.assertEqual("A", plugin.get_bootloader_state()["active"])

    def test_reset_slot_status_is_a_noop_for_a_good_slot(self):
        plugin, target = self.plugin()
        plugin.reset_slot_status("B")
        self.assertFalse([c for c in target.commands if c.startswith("fw_setenv")])

    def test_one_bad_slot_is_not_an_unhealthy_chain(self):
        plugin, target = self.plugin(order="A", left_b="0")
        plugin.check_boot_chain_healthy()
        self.assertIn("BOOT_ORDER=A", next(iter(target.artifacts.values())))

    def test_no_bootable_slot_is_an_unhealthy_chain(self):
        plugin, _ = self.plugin(order="A B", left_a="0", left_b="0")
        with self.assertRaises(AssertionError):
            plugin.check_boot_chain_healthy()

    def test_boot_confirmation_needs_the_attempt_counter_to_be_restored(self):
        plugin, target = self.plugin(booted="A", left_a="2")
        plugin.wait_until = lambda check, timeout, interval=5.0: check()  # no waiting in the unit test
        with self.assertRaises(AssertionError):
            plugin.check_boot_confirmation("A")
        target.env["BOOT_A_LEFT"] = "3"
        plugin.check_boot_confirmation("A")

    def test_mark_good_inactive_noop_is_not_supported_but_plain_rejection_is_read_from_the_config(self):
        plugin, _ = self.plugin()
        self.assertNotEqual("", plugin.unsupported_reason("mark-good-inactive-noop"))

    def test_invalid_bootname(self):
        plugin, _ = self.plugin()
        with self.assertRaises(AssertionError):
            plugin.set_primary("C")


class BaseClassTest(unittest.TestCase):
    def test_required_methods_are_not_implemented_by_default(self):
        plugin = DevicePlugin("x", FakeTarget())
        for method in (plugin.expected_system_conf, plugin.get_bootloader_state, plugin.check_boot_files):
            with self.assertRaises(NotImplementedError):
                method()


if __name__ == "__main__":
    unittest.main()
