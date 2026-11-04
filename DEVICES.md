# Adding a device

The Robot suites say *what* RAUC has to do on every device. A **device plugin** says *how* on one piece of hardware:
boot loader, partition layout, how the A/B state is read and reset. Plugins live in `libraries/devices/`; the
`DeviceUnderTest` library loads the one selected with `DEVICE` / `--device`.

Supported devices: `./run_tests.py --list-devices`. Device-specific behaviour and skipped tests are documented in the
appendix of [RAUC-TEST-PLAN.md](RAUC-TEST-PLAN.md).

## Vocabulary

| Term | Meaning |
|---|---|
| device name | Selects the plugin. Equals the Yocto `MACHINE` and the RAUC `compatible` string expected on the device |
| bootname | `"A"` or `"B"`, the `bootname=` of a slot in `system.conf` |
| slot state | `"normal"` (bootable) or `"unbootable"` |
| bootloader state | `{"current": "A", "active": "A", "slots": {"A": "normal", "B": "normal"}}`. `current` is the booted slot, `active` is the slot the boot loader tries first (RAUC's primary slot) |
| interface state | `"good"` / `"bad"`, the RAUC term for a slot state |
| feature | Optional behaviour a device may lack; the matching test is skipped with the reason |

## Steps

1. **Plugin.** Copy `libraries/devices/_template.py` to `libraries/devices/<my_device>.py` (files starting with `_` and
   `base.py` are not loaded), rename the class and set `devices = ("<machine>",)` and `description`. The module is
   discovered automatically.
2. **Implement the required methods** (they raise `NotImplementedError` in `DevicePlugin`):

   | Method | Purpose |
   |---|---|
   | `expected_system_conf()` | `{section: {key: value}}` the target's `/etc/rauc/system.conf` must contain (TC-1.2); `slot_device()` is derived from it |
   | `data_device()` | block device of the persistent data partition |
   | `check_boot_files()` | kernel/DTB/modules come with the booted rootfs (TC-2.4) |
   | `get_booted_bootname()` | slot the system booted from |
   | `get_bootloader_state()` | state dict as above, read with the boot loader's own tools |
   | `set_primary(bootname)` | make the boot loader try `bootname` first (used for restoring; must not raise on odd state) |
   | `interface_get_current/get_primary/get_state/set_primary/set_state()` | the interface RAUC uses towards the boot loader (TC-3.x) |
   | `interface_invalid_requests()` | shell commands that must fail without changing state |

3. **Optional overrides** (defaults exist):

   | Member | Purpose |
   |---|---|
   | `system_conf_placeholders()` | build-time placeholders of the recipe's `system.conf` and their values |
   | `check_installation()`, `check_partition_layout()`, `check_data_layout()` | device files, tools and layout (TC-1.1, 1.3, 1.5) |
   | `check_boot_confirmation(bootname)` | the boot loader recorded the successful boot (TC-5.4) |
   | `check_boot_chain_healthy()` | guard before reboots; default fails when any slot is `unbootable` |
   | `reset_slot_status(bootname)` | clear a sticky unbootable flag without changing the primary slot |
   | `interface_usage_request()` | `(command, usage text)` of a stand-alone interface command, `None` = n/a |
   | `unsupported` | `{feature: reason}`; features: `plain-format-rejection` (TC-6.3), `mark-good-inactive-noop` (TC-3.6) |
   | `kernel_error_regex`, `storage_log_regex`, `nic_module_regex` | kernel log patterns; an empty NIC pattern disables the check |
   | `unchanged_files` | files that an install to the inactive slot must not change |
   | `state_commands`, `diagnostic_commands` | commands recorded with the target state / for failed tests |

   Use `self.run(cmd, timeout, expected_rc)` to execute on the target and `self.save(name, text)` to store evidence.
   Never put secrets on a command line.
4. **Unit tests.** Add tests in `tests/test_devices.py` with the fakes in `tests/fakes.py` (`FakeTarget` replays
   command outputs, `FakeUBootTarget` simulates a U-Boot environment). They run without hardware:
   `.venv/bin/python -m unittest discover -s tests`.
5. **Inputs generator.** Add one line to `MACHINES` in `tools/prepare_qa_inputs.py` (`_machine(kas config, bundle
   recipe, layer dir)`) so that `--machine <device>` can collect the bundle, `system.conf`, CA and sign the negative
   bundles.
6. **Build configuration.** Add a `kas/<machine>.yml` that enables RAUC for the machine (layers, `DISTRO_FEATURES`,
   WKS with two rootfs partitions, bundle recipe).
7. **Document.** Add the device to the table in [RAUC-TEST-PLAN.md](RAUC-TEST-PLAN.md) and write its appendix section
   (boot loader, state command, slots, skipped tests, requirements such as `TARGET_POWER_CYCLE_CMD`).

## Checklist

- [ ] `./run_tests.py --list-devices` shows the device.
- [ ] `./run_tests.py --dry-run --device <name> --profile full` parses all tests.
- [ ] Unit tests pass.
- [ ] Every `NotImplementedError` of the required methods is replaced.
- [ ] Features the device lacks are listed in `unsupported` with a reason.
- [ ] Run profile `safe`, then `update`, `acceptance`, `full` on the hardware.
