# RAUC Integration Test Plan

Hardware independent test plan for the RAUC integration of a device with a redundant (A/B) root filesystem.
The test cases below describe *what* must hold on every device. Everything that differs between devices (boot
loader, partition layout, tools used to inspect the A/B state) is listed in the
[device appendix](#appendix-device-specifics) and implemented by one plugin per device
(`libraries/devices/`, see [DEVICES.md](DEVICES.md)).

Supported devices (`./run_tests.py --list-devices`):

| Device (= Yocto `MACHINE` = RAUC `compatible`) | Boot loader | Bundle recipe |
|---|---|---|
| `jetson-orin-nano-devkit-nvme` | UEFI + `nvbootctrl`, custom RAUC backend | `jetson-update-bundle` |
| `jetson-orin-nano-devkit` (SD card) | UEFI + `nvbootctrl`, custom RAUC backend | `jetson-update-bundle` |
| `raspberrypi4-64` | U-Boot, RAUC `uboot` backend | `update-bundle` |

## Conventions

- Notation: `target#` = command on the device (SSH or serial console, root login), `host$` = command on the build host.
- Let the *initial* slot be `X` and the other slot `Y` (`X` is normally `A`). `<BUNDLE>` is the `.raucb` under test.
- **Bootloader state** is the A/B state as the boot loader sees it: slot states `normal`/`unbootable`, the *active*
  slot (the one tried first = RAUC's primary slot) and the *current* slot (the one booted). How to read it per
  device is in the appendix (`<BL-STATE>` below).
- The automated suite runs the tests in the given order; later tests depend on earlier state.
- Tests that do not apply to a device are skipped with a reason (see the appendix); they are not failures.

---

## 1. Installation and static configuration

### TC-1.1 RAUC packages installed
**Description:** The image contains the RAUC client/service and the integration files of the device.
**Steps:**
```sh
target# which rauc
target# rauc --version
target# ls -l /etc/rauc/system.conf /etc/rauc/ca.cert.pem
target# <device specific files, see appendix>
```
**Pass criteria:**
- `rauc` is found and prints a version; `system.conf` and `ca.cert.pem` exist.
- The boot loader integration files and tools of the device exist (appendix).

### TC-1.2 system.conf contents
**Description:** Build-time placeholders were substituted and the slot layout matches the design of the device.
**Steps:** `target# cat /etc/rauc/system.conf`
**Pass criteria:**
- `compatible=<device name>`, no placeholder (`@...@`) remains.
- All keys of the device's expected `system.conf` (appendix) are present with the expected values; both rootfs slots
  have the expected device, `bootname` `A`/`B` and `type=ext4`.
- If `expected/system.conf` is supplied, the file matches it after substituting the placeholders.

### TC-1.3 Redundant partition layout
**Description:** The device was flashed with the A/B layout, so both rootfs partitions exist.
**Steps:** `target# lsblk -o NAME,PARTLABEL,SIZE,FSTYPE,MOUNTPOINT`, `target# findmnt -no SOURCE /`
**Pass criteria:**
- Both slot devices exist as block devices, have equal size and are not mounted twice.
- Exactly one of them is mounted on `/`.
- Device specific layout rules hold (appendix), e.g. redundant firmware partitions on the Jetson, FAT boot partition on the Raspberry Pi.

### TC-1.4 Keyring / signing chain
**Description:** The keyring on the target is the CA of the certificate that signed the bundle.
**Steps:**
```sh
target# openssl x509 -in /etc/rauc/ca.cert.pem -noout -subject -issuer -dates
host$   openssl verify -CAfile <ca.cert.pem> <development-1.cert.pem>
```
**Pass criteria:** the CA certificate is currently valid; `openssl verify` reports `OK` (when the inputs contain both certificates).

### TC-1.5 Data partition
**Description:** A persistent data partition is mounted on `/data`. RAUC never writes to it, so it is shared by both slots and survives updates.
**Steps:** `target# grep ' /data ' /proc/mounts`, `target# touch /data/x && rm /data/x`
**Pass criteria:**
- `/data` is mounted read-write, `ext4`, from the data partition of the device.
- Device specific rules hold (appendix), e.g. the Jetson `DATA` partition is the largest one and fills the disk.
- A marker file written to `/data` before every reboot of the update tests is intact afterwards (checked by the *Reboot And Verify Slot* keyword).

---

## 2. RAUC service and status

### TC-2.1 RAUC service running
**Steps:** `target# systemctl status rauc.service`, `target# journalctl -u rauc -b --no-pager | tail -n 30`
**Pass criteria:** the service is active (or D-Bus activatable); no errors about the boot loader backend, keyring or `system.conf` in the log.

### TC-2.2 `rauc status` reports both slots
**Steps:** `target# rauc status --output-format=json-pretty`
**Pass criteria:**
- `Compatible` equals the device name; the boot slot equals the slot the boot loader reports as current.
- `rootfs.0` (`A`) and `rootfs.1` (`B`) are listed, class `rootfs`, type `ext4`.
- The booted slot is `booted` with boot status `good`; the other slot is `inactive`.

### TC-2.3 Booted slot matches the mounted rootfs
**Steps:** `target# findmnt -no SOURCE /`, `target# readlink -f <slot devices>`
**Pass criteria:** the root source is the device of the booted slot and agrees with `rauc status`.

### TC-2.4 Kernel delivered with the rootfs
**Description:** The kernel (and, where the boot loader reads it from the rootfs, the DTB) and the modules of the running
kernel are in the booted rootfs, so a RAUC update replaces them together.
**Steps / criteria:** device specific (appendix, "Boot files").

---

## 3. Bootloader interface

The interface RAUC uses to read and change the A/B state is exercised directly to isolate it from the install workflow
(appendix: Jetson = `rauc-nvbootctrl-backend`, Raspberry Pi = `rauc status mark-*` and the U-Boot variables).
Calls below: `GET-CURRENT`, `GET-PRIMARY`, `GET-STATE <slot>`, `SET-PRIMARY <slot>`, `SET-STATE <slot> good|bad`.

### TC-3.1 Get-current
**Pass criteria:** prints the booted slot (`A`/`B`).

### TC-3.2 Get-primary
**Pass criteria:** prints the slot the boot loader tries first (`<BL-STATE>` active slot).

### TC-3.3 Get-state
**Pass criteria:** `good` for slots the boot loader reports `normal`, otherwise `bad`; right after a healthy boot both are `good`.

### TC-3.4 Set-primary (round trip)
**Steps:** `SET-PRIMARY Y`, `GET-PRIMARY`, `SET-PRIMARY X`, `GET-PRIMARY`.
**Pass criteria:** `GET-PRIMARY` returns the slot just set each time; `<BL-STATE>` agrees; state is restored to `X`.

### TC-3.5 Set-state good on the running slot
**Pass criteria:** exit code 0 and `GET-STATE X` returns `good`.

### TC-3.6 Set-state good on the non-running slot is a no-op
**Applies to:** devices whose interface ignores `good` for the non-running slot (Jetson). **Skipped** otherwise (appendix).
**Pass criteria:** exit code 0; slot `Y` and the active slot are not altered.

### TC-3.7 Set-state bad redirects boot to the other slot
**Steps:** `SET-PRIMARY X`, `SET-STATE X bad`, `GET-PRIMARY`, `SET-PRIMARY X`.
**Pass criteria:** after the bad call the primary is `Y`; after the restore it is `X`.

### TC-3.8 Invalid input handling
**Pass criteria:** every invalid request (unknown slot, unknown state, ...; appendix) fails with a non-zero exit code;
where the interface has a stand-alone command, calling it without arguments prints the usage text; the boot loader state is unchanged.

---

## 4. Bundle inspection

### TC-4.1 Input files verified
**Pass criteria:** the files in the inputs directory match the SHA-256 sums of `inputs.json`.

### TC-4.2 Bundle metadata
**Steps:** `rauc info --keyring=<ca.cert.pem> <BUNDLE>` (runs on the target)
**Pass criteria:** `Compatible` equals the device name; format `verity`; exactly one image (slot class `rootfs`) with a SHA-256 checksum; signature chain verified.

### TC-4.3 Bundle uses the matching rootfs
**Pass criteria:** the bundled image matches `expected/rootfs.sha256` (when supplied).

---

## 5. Update installation (X → Y)

### TC-5.1 Bundle verification on target
**Steps:** `target# rauc info <BUNDLE>`
**Pass criteria:** signature verified against `/etc/rauc/ca.cert.pem`; compatible matches; no errors.

### TC-5.2 Install to the inactive slot
**Steps:** `target# rauc install <BUNDLE>`
**Pass criteria:**
- Progress reaches 100 %, final message `installing ... succeeded`, exit code 0.
- Only the inactive slot `Y` is written: the checksums of the running system's files (`/etc/os-release`, `/usr/bin/rauc`, plus the device's list) are unchanged.
- `rauc status`: `Y` is primary, `X` still `booted`; `<BL-STATE>`: active = `Y`, current = `X`.

### TC-5.3 Reboot into the new slot
**Pass criteria:** the device boots without manual intervention, booted slot and root device are `Y`, `<BL-STATE>` current = `Y`, the data marker is intact, and (optionally, `NEW_BUILD_MARKER_*`) the new build is present.

### TC-5.4 Slot marked good after successful boot
**Description:** the new slot is confirmed after the boot. The boot loader records the successful boot (appendix) and RAUC reports `good`.
**Pass criteria:** RAUC boot status `good`; the boot confirmation of the device is recorded; no failed RAUC related units; no unbootable slot.

### TC-5.5 Second update back to the original slot (Y → X)
**Pass criteria:** the update lands in `X`, the system boots from `X` and `X` is `good`.

### TC-5.6 Persistence across update
**Pass criteria:** the data marker in `/data` survives every update. Files in the rootfs are replaced (expected). N/A if the device has no data partition.

---

## 6. Failure handling and rollback

### TC-6.1 Reject a wrongly signed bundle
**Pass criteria:** refused with a signature/keyring error; neither slot is modified and the boot configuration is unchanged.

### TC-6.2 Reject a bundle with wrong compatible string
**Pass criteria:** refused (`Compatible mismatch`); slots and boot configuration unchanged.

### TC-6.3 Reject a plain-format bundle
**Applies to:** devices whose `system.conf` sets `bundle-formats=-plain`; **skipped** otherwise (the check is made on the device).
**Pass criteria:** refused (`Bundle format 'plain' not allowed`); nothing modified.

### TC-6.4 Corrupted bundle
**Pass criteria:** the install fails with a verity/hash/signature error; the active slot and the boot order are unchanged.
The inactive slot may be left partially written (and flagged bad by the U-Boot backend), so its status is not compared.

### TC-6.5 Power loss during installation
**Steps:** start `rauc install`, cut power (`sysrq` reset) at `POWER_LOSS_PERCENT` progress.
**Pass criteria:** the device boots the previous slot `X`, `X` is good, the interrupted install did not activate `Y`, a subsequent install succeeds.

### TC-6.6 Boot failure triggers fallback
**Steps:** install a bundle whose rootfs cannot boot, reboot, power-cycle if the device hangs (`TARGET_POWER_CYCLE_CMD`), repeat until the boot loader gives up on `Y`.
**Pass criteria:**
- The device returns to slot `X`; the broken slot `Y` ends up flagged `unbootable` in the boot loader state (immediately on the Jetson, after the boot attempts are used up on devices that count them) and RAUC does not report it `good`.
- The device is not stuck permanently. The slot is re-installed by the teardown.

### TC-6.7 Mark-bad path via RAUC
**Steps:** `rauc status mark-bad`, `<BL-STATE>`, reboot, `rauc status mark-active <X>`.
**Pass criteria:** after `mark-bad` the active slot is the other one; the device boots from it; the restore works.

### TC-6.8 Manual slot switching through RAUC
**Steps:** `rauc status mark-active other`, reboot, switch back.
**Pass criteria:** the reboot enters the other slot; `rauc status` matches.

---

## 7. Robustness and system integration

### TC-7.1 Repeated update cycles (soak)
**Pass criteria:** at least `SOAK_CYCLES` (6) alternating updates; every cycle succeeds, the slot flips, no filesystem errors in `dmesg`, no mount of the inactive slot.

### TC-7.2 Filesystem health of both slots
**Pass criteria:** no filesystem/storage errors in the kernel log (patterns per device); `e2fsck -fn` of the inactive slot is clean (if installed).

### TC-7.3 Rootfs content identical to bundle image
**Pass criteria:** the first `<image size>` bytes of the written slot have the SHA-256 listed in the bundle.

### TC-7.4 Bootloader slot state consistency
**Pass criteria:** `<BL-STATE>` current = RAUC booted slot, active = RAUC primary slot, no `unbootable` slot.

### TC-7.5 No service regressions
**Pass criteria:** no failed units; no RAUC related errors in the journal; a global IP address is configured; the NIC driver of the device is loaded (warning only).

### TC-7.6 Concurrent install protection
**Pass criteria:** a second `rauc install` is rejected (busy / already in progress); the first one completes.

### TC-7.7 Installing the same bundle again
**Pass criteria:** the install succeeds into the inactive slot and the system stays bootable; the behaviour is recorded.

---

## Appendix: device specifics

The values below are implemented in `libraries/devices/<device>.py`; the plugin is the source of truth.

### Jetson Orin Nano devkit (`jetson-orin-nano-devkit-nvme`, `jetson-orin-nano-devkit`)

| Item | Value |
|---|---|
| Layer | `internal/meta-rauc-tegra-jetson-orin-nano` (kas: `kas/jetson-orin-nano.yml`, `kas/jetson-orin-nano-sd.yml`) |
| `system.conf` | `bootloader=custom`, `bundle-formats=-plain`, `data-directory=/data/rauc`, `bootloader-custom-backend=/usr/libexec/rauc-nvbootctrl-backend` |
| Slots | `/dev/disk/by-partlabel/APP` (`A`), `/dev/disk/by-partlabel/APP_b` (`B`); data: `DATA` |
| `<BL-STATE>` | `nvbootctrl dump-slots-info` (bootloader chain) merged with `nvbootctrl -t rootfs dump-slots-info` (a slot unbootable in either chain is unbootable) |
| Files (TC-1.1) | `rauc-nvbootctrl-backend` executable (`-rwxr-xr-x`), `/usr/sbin/nvbootctrl` |
| Layout (TC-1.3, 1.5) | redundant firmware partitions (`B_<name>`, `<name>_b`, `<name>_alt`; NVMe variant: failure, SD variant: warning, firmware may live in QSPI); `DATA` largest, nothing unused after it |
| Boot files (TC-2.4) | `extlinux.conf` boots `/boot/Image` and has an `FDT` line; kernel, DTB and modules are in the rootfs |
| Interface (TC-3) | `rauc-nvbootctrl-backend get-current / get-primary / get-state / set-primary / set-state`; invalid: `get-state C`, `set-primary Z`, `set-state A maybe`; without arguments: usage text |
| Boot confirmation (TC-5.4) | `nv_update_verifier.service` succeeded; no unbootable slot in either chain |
| TC-3.6 | applies (only the running slot can be verified) |
| TC-6.3 | applies (`-plain`) |
| TC-6.6 | a failed rootfs is flagged only in the rootfs chain (UEFI variables `RootfsStatusSlotA/B`) and the flag survives re-installs; the test resets it (`efivar`) |
| Kernel errors (TC-7.2) | NVMe variant: `EXT4-fs error`, `I/O error`, `nvme*` timeouts; SD variant: `mmcblk*` instead |
| NIC (TC-7.5) | `r8168`/`r8169` |

### Raspberry Pi 4 (`raspberrypi4-64`)

| Item | Value |
|---|---|
| Layers | `meta-rauc-community/meta-rauc-raspberrypi` + `meta-lts-mixins` (`scarthgap/u-boot`), kas: `kas/rpi4.yml`, bundle `update-bundle` |
| `system.conf` | `bootloader=uboot`, `data-directory=/data/`, `[keyring] path=/etc/rauc/ca.cert.pem`; no `bundle-formats` |
| Slots | `/dev/mmcblk0p2` (`A`), `/dev/mmcblk0p3` (`B`); `/boot` = FAT `mmcblk0p1`; data: `mmcblk0p5` (no partition labels on the MBR table) |
| `<BL-STATE>` | `fw_printenv`: `BOOT_ORDER` (slots in order), `BOOT_A_LEFT` / `BOOT_B_LEFT` (remaining boot attempts, hexadecimal). A slot is `normal` when it is in `BOOT_ORDER` and has attempts left; the active slot is the first such slot; the current slot is `rauc.slot=` on the kernel command line |
| Files (TC-1.1) | `fw_printenv`, `fw_setenv`, `/etc/fw_env.config`, `/boot/uboot.env`, `rauc.slot=` on the command line |
| Layout (TC-1.3, 1.5) | `/boot` is the vfat partition `mmcblk0p1`; data on `mmcblk0p5` |
| Boot files (TC-2.4) | U-Boot loads `/boot/Image` from the booted rootfs. The FAT `/boot` hides it, so the check bind mounts `/` and looks at that copy. The **DTB comes from the firmware on the boot partition and is not part of the update** |
| Interface (TC-3) | RAUC's own U-Boot backend through `rauc status mark-active/mark-good/mark-bad <slot>` and `rauc status` (no stand-alone command: the usage check is not applicable); invalid: unknown slot or unknown `mark-*` verb |
| Boot confirmation (TC-5.4) | `rauc-mark-good.service` restored `BOOT_<slot>_LEFT` to 3 |
| TC-3.6 | **skipped**: the U-Boot backend restores the attempt counter of any slot on `mark-good`, so there is no no-op |
| TC-6.3 | **skipped** unless `bundle-formats` of `system.conf` excludes `plain` (not set in the stock community `system.conf`) |
| TC-6.4 | slot status not compared: RAUC marks the slot it is about to write as bad (removed from `BOOT_ORDER`, attempts 0) |
| TC-6.6 | a broken boot hangs in U-Boot (`boot.scr` never resets when the kernel cannot be loaded): set `TARGET_POWER_CYCLE_CMD`. `boot.scr` decrements `BOOT_<slot>_LEFT` at every try, so a broken slot costs the reboot plus 2 power cycles (the 3 tries); the 3rd power cycle boots the other slot. The test repeats until the slot is `unbootable` |
| Teardowns | the inactive slot is re-added to `BOOT_ORDER` with full attempts (U-Boot environment variables), the primary is restored |
| Kernel errors (TC-7.2) | `EXT4-fs error`, `I/O error`, `mmcblk*` timeouts/errors |
| NIC (TC-7.5) | not checked |

---

## Result sheet

| ID | Title | Result | Notes |
|---|---|---|---|
| TC-1.1 | RAUC packages installed | | |
| TC-1.2 | system.conf contents | | |
| TC-1.3 | Redundant partition layout | | |
| TC-1.4 | Keyring / signing chain | | |
| TC-1.5 | Data partition | | |
| TC-2.1 | RAUC service running | | |
| TC-2.2 | `rauc status` both slots | | |
| TC-2.3 | Booted slot matches rootfs | | |
| TC-2.4 | Kernel delivered with the rootfs | | |
| TC-3.1 | Interface get-current | | |
| TC-3.2 | Interface get-primary | | |
| TC-3.3 | Interface get-state | | |
| TC-3.4 | Interface set-primary | | |
| TC-3.5 | Interface set-state good (running) | | |
| TC-3.6 | Interface set-state good (other) | | |
| TC-3.7 | Interface set-state bad | | |
| TC-3.8 | Interface invalid input | | |
| TC-4.1 | Input files verified | | |
| TC-4.2 | Bundle metadata | | |
| TC-4.3 | Bundle uses matching rootfs | | |
| TC-5.1 | Bundle verification on target | | |
| TC-5.2 | Install to inactive slot | | |
| TC-5.3 | Reboot into new slot | | |
| TC-5.4 | Slot marked good | | |
| TC-5.5 | Update back | | |
| TC-5.6 | Persistence across update | | |
| TC-6.1 | Reject wrongly signed bundle | | |
| TC-6.2 | Reject wrong compatible | | |
| TC-6.3 | Reject plain bundle | | |
| TC-6.4 | Corrupted bundle | | |
| TC-6.5 | Power loss during install | | |
| TC-6.6 | Boot failure fallback | | |
| TC-6.7 | `mark-bad` path | | |
| TC-6.8 | Manual slot switching | | |
| TC-7.1 | Repeated update cycles | | |
| TC-7.2 | Filesystem health | | |
| TC-7.3 | Rootfs identical to bundle | | |
| TC-7.4 | Bootloader slot consistency | | |
| TC-7.5 | No service regressions | | |
| TC-7.6 | Concurrent install protection | | |
| TC-7.7 | Re-install same bundle | | |

**Overall acceptance:** the integration is successful when all tests in sections 1-5 pass, TC-6.1 - TC-6.5 pass (skips
listed in the appendix excepted), and TC-6.6 demonstrates a fallback to the previous slot. Any FAIL must be recorded
with logs (`journalctl -u rauc`, the `<BL-STATE>` output, serial console output).
