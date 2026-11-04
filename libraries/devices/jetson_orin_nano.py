"""NVIDIA Jetson Orin Nano devkit (meta-tegra + meta-rauc-tegra-jetson-orin-nano).

Boot chain: UEFI (L4TLauncher) with the Tegra A/B slot logic.  RAUC uses the ``custom`` bootloader
backend ``/usr/libexec/rauc-nvbootctrl-backend`` which maps RAUC's calls onto ``nvbootctrl``.

* Slots are GPT partitions addressed by partition label (``APP`` / ``APP_b``), data is ``DATA``.
* Kernel, DTB and modules live in the rootfs and are loaded through ``/boot/extlinux/extlinux.conf``.
* ``nvbootctrl`` knows two independent slot chains: ``bootloader`` and ``rootfs``.  A rootfs that
  fails to boot is only flagged ``unbootable`` in the rootfs chain and the flag survives re-installs
  (it lives in the UEFI variables ``RootfsStatusSlotA/B``).
* ``nv_update_verifier.service`` confirms a successful boot (``nvbootctrl verify``).
"""

from __future__ import annotations

import re
import time

from .base import BOOTNAMES, DevicePlugin, check_bootname

BACKEND = "/usr/libexec/rauc-nvbootctrl-backend"
# GUID of the UEFI variables RootfsStatusSlotA / RootfsStatusSlotB.
NVIDIA_GUID = "781e084c-a330-417c-b678-38e696380cb9"
PARTLABELS = {"A": "APP", "B": "APP_b"}
TMP = "/tmp"


def parse_nvbootctrl_slots(text: str) -> dict:
    """Parse ``nvbootctrl [-t chain] dump-slots-info``.

    Returns ``{current: 'A', active: 'A', slots: {'A': 'normal', 'B': 'normal'}}``.
    """
    cur = re.search(r"Current (?:bootloader|rootfs) slot:\s*([AB])", text)
    act = re.search(r"Active (?:bootloader|rootfs) slot:\s*([AB])", text)
    # Rootfs output may contain 'retry_count: N,' between slot and status.
    by_index = {
        int(i): s
        for i, s in re.findall(r"^slot:\s*(\d+),(?:\s*retry_count:\s*\d+,)?\s*status:\s*(\w+)", text, re.M)
    }
    if not cur or not act or not by_index:
        raise AssertionError(f"Unexpected 'nvbootctrl dump-slots-info' output:\n{text}")
    return {
        "current": cur.group(1),
        "active": act.group(1),
        "slots": {"A": by_index.get(0), "B": by_index.get(1)},
    }


class _JetsonOrinNano(DevicePlugin):
    """Behaviour shared by all Orin Nano devkit variants (not registered itself)."""

    unchanged_files = ("/usr/sbin/nvbootctrl",)
    nic_module_regex = "r8168|r8169"
    state_commands = (
        "nvbootctrl dump-slots-info",
        "nvbootctrl -t rootfs dump-slots-info",
        "ls -l /dev/disk/by-partlabel/",
        f"sha256sum {BACKEND}",
    )
    diagnostic_commands = ("nvbootctrl dump-slots-info", "nvbootctrl -t rootfs dump-slots-info")
    #: Only the NVMe layout is known to carry redundant firmware partitions (esp_alt, ...) on the
    #: storage device; elsewhere a missing pair is only reported (see check_partition_layout).
    strict_firmware_pairs = True

    # --------------------------------------------------------------- description
    def expected_system_conf(self) -> dict[str, dict[str, str]]:
        return {
            "system": {
                "compatible": self.machine,
                "bootloader": "custom",
                "bundle-formats": "-plain",
                "data-directory": f"{self.data_mount}/rauc",
            },
            "handlers": {"bootloader-custom-backend": BACKEND},
            "slot.rootfs.0": {"device": "/dev/disk/by-partlabel/APP", "bootname": "A", "type": "ext4"},
            "slot.rootfs.1": {"device": "/dev/disk/by-partlabel/APP_b", "bootname": "B", "type": "ext4"},
        }

    def system_conf_placeholders(self) -> dict[str, str]:
        return {"@RAUC_COMPATIBLE@": self.machine, "@DATA_MOUNT_POINT@": self.data_mount}

    def data_device(self) -> str:
        return "/dev/disk/by-partlabel/DATA"

    # -------------------------------------------------------------------- checks
    def check_installation(self) -> None:
        listing = self.run(f"ls -l {BACKEND}").stdout
        if not re.match(r"-rwxr-xr-x", listing):
            raise AssertionError(f"{BACKEND} must be an executable regular file (rwxr-xr-x): {listing}")
        self.run(f"test -x {BACKEND}")
        self.run("command -v nvbootctrl")
        self.run("test -x /usr/sbin/nvbootctrl")

    def check_partition_layout(self) -> None:
        labels = self.run("ls -1 /dev/disk/by-partlabel/").stdout.split()
        for label in PARTLABELS.values():
            if label not in labels:
                raise AssertionError(f"Partition label {label} not found in {labels}")
        # Bootloader / firmware partitions come in A/B pairs as well; the secondary copy is named
        # B_<name>, <name>_b or <name>_alt (e.g. esp_alt, recovery_alt, recovery-dtb_alt).
        paired = [x for x in labels if re.search(r"(?i)(^B_|_b$|_alt$)", x) and x != "APP_b"]
        if not paired:
            message = "No redundant bootloader/firmware partitions found"
            if self.strict_firmware_pairs:
                raise AssertionError(message)
            self.warn(f"{message} on the storage device (the firmware may live in QSPI flash)")

    def check_data_layout(self) -> None:
        self.run("test -b /dev/disk/by-partlabel/DATA")
        # No unallocated space after DATA, only the secondary GPT and alignment may remain (sectors).
        free = self.run(
            'p=$(basename "$(readlink -f /dev/disk/by-partlabel/DATA)"); '
            'd=$(basename "$(readlink -f /sys/class/block/$p/..)"); '
            "echo $(( $(cat /sys/class/block/$d/size) - $(cat /sys/class/block/$p/start) "
            "- $(cat /sys/class/block/$p/size) ))"
        ).stdout.strip()
        if not 0 <= int(free) <= 4096:
            raise AssertionError(f"{free} sectors are left unused after the DATA partition")
        # DATA is the largest partition (it takes the space not used by the rootfs slots).
        largest = self.run(
            'for f in /sys/class/block/*/partition; do p=$(dirname $f); echo "$(cat $p/size) $(basename $p)"; '
            "done | sort -n | tail -n 1"
        ).stdout.split()[1]
        data = self.block_name("/dev/disk/by-partlabel/DATA")
        if largest != data:
            raise AssertionError(f"DATA ({data}) is not the largest partition ({largest})")

    def check_boot_files(self) -> None:
        """L4TLauncher loads kernel, initrd and DTB through extlinux.conf of the booted rootfs.

        The kernel DTB comes from the rootfs only when extlinux.conf has an FDT line (UBOOT_EXTLINUX_FDT).
        """
        conf = self.run("cat /boot/extlinux/extlinux.conf").stdout
        if not re.search(r"(?m)^\s*LINUX\s+/boot/Image\s*$", conf):
            raise AssertionError(f"extlinux.conf does not boot /boot/Image:\n{conf}")
        fdt = re.search(r"(?m)^\s*FDT\s+(\S+\.dtb)\s*$", conf)
        if not fdt:
            raise AssertionError("extlinux.conf has no FDT line; the kernel DTB is not delivered with the rootfs")
        self.run(f"test -s {fdt.group(1)}")
        uname = self.run("uname -r").stdout.strip()
        self.run(f'test -s /boot/Image && grep -c -- "{uname}" /boot/Image', timeout=120)
        self.run(f"test -d /lib/modules/{uname}")

    def check_boot_confirmation(self, bootname: str) -> None:
        # Without nv_update_verifier 'nvbootctrl verify' never runs and the bootloader retry counters
        # are never cleared (slots fail over, eventually unbootable).
        out = self.run(
            "systemctl show nv_update_verifier.service -p LoadState -p Result -p ExecMainStatus"
        ).stdout
        for needle, why in (
            ("LoadState=loaded", "nv_update_verifier.service is not installed"),
            ("Result=success", "nv_update_verifier.service did not succeed"),
            ("ExecMainStatus=0", "nv_update_verifier.service exited non-zero"),
        ):
            if needle not in out:
                raise AssertionError(f"{why}:\n{out}")
        self.run("nvbootctrl dump-slots-info")
        self.check_boot_chain_healthy()

    def check_boot_chain_healthy(self) -> None:
        """Neither the bootloader nor the rootfs slot chain may contain an unbootable slot.

        The bootloader fails slots over (and finally boots the network-less recovery kernel) when
        'nvbootctrl verify' is not run, so both chains are checked and saved as evidence.
        """
        for chain in ("bootloader", "rootfs"):
            r = self.run(f"nvbootctrl -t {chain} dump-slots-info", timeout=60, expected_rc="any")
            self.save(f"nvbootctrl-{chain}-{time.strftime('%H%M%S')}.txt", f"{r.stdout}\n{r.stderr}")
            bad = [ln for ln in r.stdout.splitlines() if re.search(r"(?i)^slot:.*status:\s*unbootable", ln)]
            if bad:
                raise AssertionError(f"Unbootable {chain} slot:\n{r.stdout}")

    # -------------------------------------------------------------------- state
    def _chain(self, chain: str, expected_rc: int | str = 0) -> dict:
        r = self.run(f"nvbootctrl -t {chain} dump-slots-info", timeout=60, expected_rc=expected_rc)
        return parse_nvbootctrl_slots(r.stdout)

    def get_booted_bootname(self) -> str:
        index = self.run("nvbootctrl get-current-slot").stdout.strip()
        return BOOTNAMES[int(index)]

    def get_bootloader_state(self) -> dict:
        """Bootloader chain, with slots flagged unbootable in the rootfs chain merged in.

        A rootfs that failed to boot is only flagged in the rootfs chain; the bootloader chain keeps
        reporting 'normal'.  The merged view is what the RAUC backend reports as good/bad.
        """
        info = self._chain("bootloader")
        slots = dict(info["slots"])
        try:
            rootfs = self._chain("rootfs", expected_rc="any")
        except AssertionError:
            rootfs = None  # rootfs chain not available
        if rootfs:
            for bootname in BOOTNAMES:
                if rootfs["slots"][bootname] == "unbootable":
                    slots[bootname] = "unbootable"
        return {"current": info["current"], "active": info["active"], "slots": slots}

    def set_primary(self, bootname: str) -> None:
        self.run(f"{BACKEND} set-primary {check_bootname(bootname)}", expected_rc="any")

    def reset_slot_status(self, bootname: str) -> None:
        """Clear the 'unbootable' flag of a slot in the rootfs chain.

        Neither ``nvbootctrl set-active-boot-slot`` nor a reboot clears it, so the UEFI variable is
        reset (0 = normal).
        """
        check_bootname(bootname)
        if self._chain("rootfs")["slots"][bootname] != "unbootable":
            return
        self.warn(f"Resetting rootfs status of slot {bootname}")
        zero = f"{TMP}/rauc-test-zero.bin"
        self.run(
            f"printf '\\000\\000\\000\\000' > {zero} && "
            f"efivar -n {NVIDIA_GUID}-RootfsStatusSlot{bootname} -w -f {zero} -A 7 && rm -f {zero}"
        )
        status = self._chain("rootfs")["slots"][bootname]
        if status != "normal":
            raise AssertionError(f"Rootfs status of slot {bootname} could not be reset (now {status})")

    # ------------------------------------------------------ bootloader interface
    def interface_get_current(self) -> str:
        return self.run(f"{BACKEND} get-current").stdout.strip()

    def interface_get_primary(self) -> str:
        return self.run(f"{BACKEND} get-primary").stdout.strip()

    def interface_get_state(self, bootname: str) -> str:
        return self.run(f"{BACKEND} get-state {check_bootname(bootname)}").stdout.strip()

    def interface_set_primary(self, bootname: str) -> None:
        self.run(f"{BACKEND} set-primary {check_bootname(bootname)}")

    def interface_set_state(self, bootname: str, state: str) -> None:
        self.run(f"{BACKEND} set-state {check_bootname(bootname)} {state}")

    def interface_invalid_requests(self) -> list[str]:
        return [f"{BACKEND} get-state C", f"{BACKEND} set-primary Z", f"{BACKEND} set-state A maybe"]

    def interface_usage_request(self) -> tuple[str, str] | None:
        return (BACKEND, "Usage")


class JetsonOrinNanoNvme(_JetsonOrinNano):
    devices = ("jetson-orin-nano-devkit-nvme",)
    description = "NVIDIA Jetson Orin Nano devkit, rootfs on NVMe (UEFI + nvbootctrl, custom RAUC backend)"
    kernel_error_regex = r"EXT4-fs error|I/O error|nvme[0-9a-z]*:.*(timeout|reset|fail|error)"
    storage_log_regex = r"ext4|i/o error|nvme"


class JetsonOrinNanoSd(_JetsonOrinNano):
    devices = ("jetson-orin-nano-devkit",)
    description = "NVIDIA Jetson Orin Nano devkit, rootfs on SD card (UEFI + nvbootctrl, custom RAUC backend)"
    kernel_error_regex = r"EXT4-fs error|I/O error|mmcblk[0-9]+.*(timeout|error)"
    storage_log_regex = r"ext4|i/o error|mmcblk"
    strict_firmware_pairs = False
