# RAUC Integration Test Plan – Jetson Orin Nano Devkit (NVMe)

Verifies the RAUC integration implemented in `internal/meta-jetson-rauc`:

| Item | Value |
|---|---|
| Machine / compatible | `jetson-orin-nano-devkit-nvme` (`RAUC_COMPATIBLE = ${MACHINE}`) |
| Slots | `rootfs.0` → `/dev/disk/by-partlabel/APP` (bootname `A`), `rootfs.1` → `/dev/disk/by-partlabel/APP_b` (bootname `B`) |
| Bootloader backend | `custom` → `/usr/libexec/rauc-nvbootctrl-backend` (wraps `nvbootctrl`) |
| Bundle | `jetson-update-bundle`, format `verity`, plain bundles disabled |
| Keyring | `/etc/rauc/ca.cert.pem`; bundle signed with the development key (`development-1.*.pem`) |
| Flash layout | Redundant (`USE_REDUNDANT_FLASH_LAYOUT_DEFAULT = "1"`) |

## Conventions

- Commands prefixed with `host$` run on the build host, `target#` on the Jetson (SSH or serial console, root login via `debug-tweaks`).
- `<BUNDLE>` = `jetson-update-bundle-jetson-orin-nano-devkit-nvme.raucb` (found under `build/tmp-glibc-glibc/deploy/images/jetson-orin-nano-devkit-nvme/`).
- Record the result of each test as PASS / FAIL in the [result sheet](#result-sheet).
- Run tests in the given order; later tests depend on earlier state. Let the *initial* slot be `X` and the other slot `Y` (`X` is normally `A`).

---

## 1. Installation and static configuration

### TC-1.1 RAUC packages installed
**Description:** The image contains the RAUC client/service and the integration files.
**Steps:**
```sh
target# which rauc
target# rauc --version
target# ls -l /etc/rauc/system.conf /etc/rauc/ca.cert.pem /usr/libexec/rauc-nvbootctrl-backend
target# which nvbootctrl
```
**Pass criteria:**
- `rauc` is found and prints a version.
- All three files exist; `rauc-nvbootctrl-backend` is executable (`-rwxr-xr-x`).
- `nvbootctrl` exists at `/usr/sbin/nvbootctrl`.

### TC-1.2 system.conf contents
**Description:** The `@RAUC_COMPATIBLE@` and `@DATA_MOUNT_POINT@` placeholders were substituted and the slot layout matches the design.
**Steps:**
```sh
target# cat /etc/rauc/system.conf
```
**Pass criteria:**
- `compatible=jetson-orin-nano-devkit-nvme` (no `@RAUC_COMPATIBLE@` remains).
- `bootloader=custom`, `bundle-formats=-plain`.
- `data-directory=/data/rauc` (`DATA_MOUNT_POINT` substituted; no `@DATA_MOUNT_POINT@` remains).
- If `expected/system.conf` is supplied, the file matches it after substituting both placeholders.
- `bootloader-custom-backend=/usr/libexec/rauc-nvbootctrl-backend`.
- `[slot.rootfs.0]` → `APP`, bootname `A`; `[slot.rootfs.1]` → `APP_b`, bootname `B`; both `type=ext4`.

### TC-1.3 Redundant partition layout
**Description:** The device was flashed with the A/B layout, so both rootfs partitions exist.
**Steps:**
```sh
target# ls -l /dev/disk/by-partlabel/
target# lsblk -o NAME,PARTLABEL,SIZE,FSTYPE,MOUNTPOINT
```
**Pass criteria:**
- `APP` and `APP_b` both exist, have equal size and are not mounted twice.
- Exactly one of them is mounted on `/`.
- Redundant bootloader/firmware partitions exist as well. The secondary copy is named `B_<name>`, `<name>_b` or `<name>_alt` (e.g. `esp`/`esp_alt`, `recovery`/`recovery_alt`, `recovery-dtb`/`recovery-dtb_alt`).

### TC-1.4 Keyring / signing chain
**Description:** The keyring on the target is the CA of the certificate that signed the bundle.
**Steps:**
```sh
target# openssl x509 -in /etc/rauc/ca.cert.pem -noout -subject -issuer -dates
host$   openssl verify -CAfile internal/meta-jetson-rauc/recipes-core/rauc/files/ca.cert.pem \
          internal/meta-jetson-rauc/recipes-core/bundles/files/development-1.cert.pem
```
**Pass criteria:**
- CA certificate is currently valid (not expired).
- `openssl verify` reports `development-1.cert.pem: OK`.

### TC-1.5 Data partition
**Description:** The `DATA` partition fills the space left on the disk after the A/B layout and is mounted on `/data`.
RAUC never writes to it, so it is shared by both slots and survives updates.
**Steps:**
```sh
target# ls -l /dev/disk/by-partlabel/DATA
target# lsblk -o NAME,PARTLABEL,SIZE,FSTYPE,MOUNTPOINT
target# grep ' /data ' /proc/mounts
target# touch /data/x && rm /data/x
```
**Pass criteria:**
- `DATA` exists, is the largest partition and no space (beyond the secondary GPT/alignment) is left after it.
- `/data` is mounted read-write, `ext4`, from the `DATA` partition.
- A marker file written to `/data` before every reboot of the update tests is intact afterwards (checked by the
  *Reboot And Verify Slot* keyword).

---

## 2. RAUC service and status

### TC-2.1 RAUC service running
**Description:** The RAUC D-Bus service starts and is reachable.
**Steps:**
```sh
target# systemctl status rauc.service
target# systemctl is-enabled rauc.service
target# journalctl -u rauc -b --no-pager | tail -n 30
```
**Pass criteria:**
- Service is `active (running)` (or D-Bus activatable and starts on first `rauc status` call).
- No errors about bootloader backend, keyring or `system.conf` in the log.

### TC-2.2 `rauc status` reports both slots
**Description:** RAUC correctly maps both slots and the booted slot.
**Steps:**
```sh
target# rauc status
target# rauc status --output-format=json-pretty
```
**Pass criteria:**
- `Compatible: jetson-orin-nano-devkit-nvme`, `Boot Slot` = `A` (or `B`, matching `nvbootctrl get-current-slot` 0/1).
- `rootfs.0` (bootname `A`) and `rootfs.1` (bootname `B`) are listed, class `rootfs`, type `ext4`.
- The booted slot has `state: booted`, `boot status: good`; the other slot is `inactive`.
- `Activated:` shows the booted slot.

### TC-2.3 Booted slot matches the mounted rootfs
**Description:** RAUC's view of the booted slot equals the real root device.
**Steps:**
```sh
target# findmnt -no SOURCE /
target# readlink -f /dev/disk/by-partlabel/APP /dev/disk/by-partlabel/APP_b
target# nvbootctrl get-current-slot
```
**Pass criteria:**
- Root source equals `APP` (slot `0`/`A`) or `APP_b` (slot `1`/`B`) according to `nvbootctrl get-current-slot`, and agrees with `rauc status`.

---

## 3. Bootloader backend (`rauc-nvbootctrl-backend`)

Each command is executed directly to isolate the backend from RAUC. Use `BE=/usr/libexec/rauc-nvbootctrl-backend`.

### TC-3.1 `get-current`
**Steps:** `target# $BE get-current; echo rc=$?`
**Pass criteria:** Prints `A` or `B` matching the booted slot; `rc=0`.

### TC-3.2 `get-primary`
**Steps:** `target# $BE get-primary; echo rc=$?; nvbootctrl dump-slots-info`
**Pass criteria:** Prints the slot shown as `Active bootloader slot` in `dump-slots-info`; `rc=0`.

### TC-3.3 `get-state`
**Steps:**
```sh
target# $BE get-state A; $BE get-state B
target# nvbootctrl dump-slots-info
```
**Pass criteria:**
- Prints `good` for slots with `status: normal`, otherwise `bad`.
- Right after a healthy boot both slots are normally `good`.

### TC-3.4 `set-primary` (round trip)
**Description:** Primary slot can be switched and restored.
**Steps:**
```sh
target# $BE set-primary Y; $BE get-primary      # Y = inactive slot name
target# nvbootctrl dump-slots-info              # Active bootloader slot -> Y
target# $BE set-primary X; $BE get-primary      # restore
```
**Pass criteria:** `get-primary` returns the slot just set each time; `rc=0`; `nvbootctrl` agrees. State is restored to `X` at the end.

### TC-3.5 `set-state good` on the running slot
**Steps:** `target# $BE set-state X good; echo rc=$?; $BE get-state X`
**Pass criteria:** `rc=0` and `get-state X` returns `good`.

### TC-3.6 `set-state good` on the non-running slot is a no-op
**Steps:** `target# $BE set-state Y good; echo rc=$?; nvbootctrl dump-slots-info`
**Pass criteria:** `rc=0`; status of slot `Y` is not altered (design: only the running slot can be verified).

### TC-3.7 `set-state bad` redirects boot to the other slot
**Description:** Marking the *active* slot bad switches the active slot to the other one.
**Steps:**
```sh
target# $BE get-primary                # expect X
target# $BE set-state X bad; echo rc=$?
target# $BE get-primary                # expect Y
target# $BE set-primary X              # restore
```
**Pass criteria:** After `set-state X bad`, primary is `Y`. After restore, primary is `X`.

### TC-3.8 Invalid input handling
**Steps:**
```sh
target# $BE get-state C; echo rc=$?
target# $BE set-primary Z; echo rc=$?
target# $BE set-state A maybe; echo rc=$?
target# $BE; echo rc=$?
```
**Pass criteria:** Every call returns a non-zero exit code; the last prints the usage text on stderr; no `nvbootctrl` state changes.

---

## 4. Bundle build and inspection (build host)

### TC-4.1 Bundle builds
**Steps:**
```sh
host$ kas build kas/jetson-orin-nano.yml --target jetson-update-bundle
host$ ls -lh build/tmp-glibc-glibc/deploy/images/jetson-orin-nano-devkit-nvme/*.raucb
```
**Pass criteria:** Build finishes without errors; a `.raucb` file exists with a size comparable to the compressed rootfs (non-zero, hundreds of MB).

### TC-4.2 Bundle metadata
**Steps:**
```sh
host$ rauc info --keyring=internal/meta-jetson-rauc/recipes-core/rauc/files/ca.cert.pem <BUNDLE>
```
**Pass criteria:**
- `Compatible: jetson-orin-nano-devkit-nvme`.
- `Bundle Format: verity`.
- Exactly one image: slot class `rootfs`, filename `core-image-minimal-*.ext4`, with a SHA-256 checksum.
- Signature chain verified (`Certificate Chain` shows development cert signed by the CA).

### TC-4.3 Bundle uses the matching rootfs
**Description:** The bundled rootfs corresponds to the image that is currently flashed (same build or intentionally newer).
**Steps:** Compare the `/etc/os-release`/build timestamp of the image with the bundle’s rootfs (`rauc extract` + mount, or `rauc info` version/build fields).
**Pass criteria:** Bundle content is from the expected build; version difference is identifiable (needed to confirm the update in section 5).

---

## 5. Update installation (A → B)

Preparation: copy the bundle to the target, optionally after making an identifiable change in the new build (e.g. changed `/etc/issue` or version string), so a successful update is visible.

```sh
host$   scp <BUNDLE> root@<target-ip>:/tmp/
target# df -h /tmp          # enough space for the bundle
```

### TC-5.1 Bundle verification on target
**Steps:** `target# rauc info /tmp/<BUNDLE>`
**Pass criteria:** Signature verified against `/etc/rauc/ca.cert.pem`; compatible matches; no errors.

### TC-5.2 Install to the inactive slot
**Steps:**
```sh
target# rauc install /tmp/<BUNDLE>
```
**Pass criteria:**
- Progress reaches 100 %; final message `installing ... succeeded`; exit code `0`.
- Only the **inactive** slot (`Y`) is written; the running slot is untouched (compare `md5sum` of a few files or check that the system keeps running normally).
- `rauc status` afterwards shows `Y` as primary/activated, boot status `good` (or pending) as designed, and `X` still `booted`.
- `nvbootctrl dump-slots-info`: `Active bootloader slot` = `Y`, `Current bootloader slot` = `X`.

### TC-5.3 Reboot into the new slot
**Steps:**
```sh
target# reboot
# after boot:
target# rauc status
target# nvbootctrl get-current-slot
target# findmnt -no SOURCE /
```
**Pass criteria:**
- System boots to a login prompt/SSH without manual intervention.
- Booted slot is now `Y`; root device is the partition of `Y`.
- The identifiable change from the new build is present.

### TC-5.4 Slot marked good after successful boot
**Description:** `nv_update_verifier` / `rauc-mark-good` confirm the new slot.
**Steps:**
```sh
target# systemctl status nv_update_verifier.service
target# systemctl status rauc-mark-good.service     # if present in the image
target# nvbootctrl dump-slots-info
target# rauc status
```
**Pass criteria:** After boot, slot `Y` has `status: normal` and RAUC shows boot status `good`. No services failed related to rauc/nv-update-verifier.

### TC-5.5 Second update back to the original slot (B → A)
**Steps:** Repeat TC-5.2 – TC-5.4 with the bundle.
**Pass criteria:** Update lands in `X`, the system boots from `X`, and slot `X` is `good`. Proves the mechanism works in both directions.

### TC-5.6 Persistence across update (if applicable)
**Description:** Data outside the rootfs is not affected by an update (note: with this minimal setup there is no separate data partition; the rootfs is replaced entirely).
**Steps:** Before update, create `/tmp` and `/root/testfile` markers; after update check them.
**Pass criteria:** Documented behavior is observed – files in `/root` are **lost** on the rootfs (expected for the current layout, unless a data partition/overlay is added). Mark N/A if a persistent partition is added.

---

## 6. Failure handling and rollback

### TC-6.1 Reject an unsigned / wrongly signed bundle
**Steps:**
```sh
host$   # build or re-sign a bundle with a different key/cert
host$   rauc bundle --cert=other.cert.pem --key=other.key.pem <input-dir> bad.raucb
target# rauc install /tmp/bad.raucb
```
**Pass criteria:** Installation is refused with a signature/keyring verification error; neither slot is modified; active slot unchanged.

### TC-6.2 Reject a bundle with wrong compatible string
**Steps:** Create a bundle whose `compatible` is `other-machine` (signed with the dev key), install it.
**Pass criteria:** Installation is refused (`Compatible mismatch`); slots unchanged.

### TC-6.3 Reject a plain-format bundle
**Description:** `bundle-formats=-plain` prevents the legacy format.
**Steps:** Install a bundle created with `format=plain`.
**Pass criteria:** Installation is refused due to disallowed bundle format.

### TC-6.4 Corrupted bundle
**Steps:**
```sh
target# cp /tmp/<BUNDLE> /tmp/corrupt.raucb
target# dd if=/dev/urandom of=/tmp/corrupt.raucb bs=1M seek=10 count=1 conv=notrunc
target# rauc install /tmp/corrupt.raucb
```
**Pass criteria:** Install fails with a verity/hash/signature error; the inactive slot is not marked as bootable/activated; the running system stays unchanged.

### TC-6.5 Power loss during installation
**Description:** Interrupting the write does not brick the device.
**Steps:** Start `rauc install`, and after ~30–50 % progress cut power (or `echo b > /proc/sysrq-trigger`).
**Pass criteria:** Device boots again into the **previous** (still valid) slot `X`; `rauc status` shows `X` booted and good; a subsequent install succeeds.

### TC-6.6 Boot failure triggers fallback
**Description:** A new slot that fails to boot is not retried forever; the bootloader falls back to the old slot.
**Steps:**
1. Install a deliberately broken bundle (e.g. rootfs with a broken `/sbin/init` or kernel panic via `init=/bin/false` change) into `Y`.
2. Reboot and let the device attempt booting (power-cycle if it hangs).
3. Observe over several boots; check `nvbootctrl dump-slots-info` from the recovered slot.
**Pass criteria:**
- After the Jetson's retry counter is exhausted, it boots slot `X` again (retry/fall-back behavior of the UEFI boot chain).
- `rauc status` from `X` shows `Y` as `bad`/not good.
- Device is not stuck permanently.

> Note: `set-state bad` cannot mark a slot unbootable from user space (see backend comment). The fallback relies on the NVIDIA boot chain retry counters, which is what this test validates.

### TC-6.7 Mark-bad path via RAUC
**Steps:**
```sh
target# rauc status mark-bad            # marks the booted slot bad
target# nvbootctrl dump-slots-info
target# reboot
target# rauc status
```
**Pass criteria:** After `mark-bad`, the active slot is the other slot. After reboot, the device boots from the other slot. Afterwards restore with `rauc status mark-active <slot>` and verify.

### TC-6.8 Manual slot switching through RAUC
**Steps:**
```sh
target# rauc status mark-active other
target# nvbootctrl dump-slots-info
target# reboot
target# rauc status mark-active booted   # restore on the other side as required
```
**Pass criteria:** Reboot enters the other slot; `rauc status` matches; no data corruption.

---

## 7. Robustness and system integration

### TC-7.1 Repeated update cycles (soak)
**Steps:** Perform at least 5 alternating updates (A→B→A→B→A→B), rebooting each time and checking `rauc status`.
**Pass criteria:** Every cycle succeeds; slot always flips; no filesystem errors in `dmesg`; no leftover mounts of the inactive slot.

### TC-7.2 Filesystem health of both slots
**Steps:**
```sh
target# dmesg | grep -iE 'ext4|i/o error|nvme' | tail
target# e2fsck -fn /dev/disk/by-partlabel/<inactive>     # read-only check on the inactive slot
```
**Pass criteria:** No EXT4 or NVMe I/O errors; `e2fsck` of the freshly written slot reports clean.

### TC-7.3 Rootfs content identical to bundle image
**Steps:** After the update, compare the hash of the inactive-slot image written by RAUC with the image in the bundle:
```sh
host$   sha256sum <extracted core-image-minimal.ext4>
target# sha256sum /dev/disk/by-partlabel/<slot-written>   # compare only the first <image size> bytes (e.g. head -c <size> | sha256sum)
```
**Pass criteria:** Checksums match for the image-size range.

### TC-7.4 Bootloader slot state consistency
**Steps:** After each update/reboot run `nvbootctrl dump-slots-info` and `rauc status`.
**Pass criteria:** `Current bootloader slot` = RAUC's booted slot; `Active bootloader slot` = RAUC's primary slot; no `unbootable` slot after a healthy update.

### TC-7.5 No service regressions after update
**Steps:**
```sh
target# systemctl --failed
target# journalctl -b -p err --no-pager
target# ip a; ssh into the target
```
**Pass criteria:** No failed units; networking (including `r8168` NIC) and SSH work; no RAUC-related errors.

### TC-7.6 Concurrent install protection
**Steps:** Start `rauc install` and, in a second shell, start another `rauc install`.
**Pass criteria:** The second call is rejected with "Installation already in progress"/busy message; first one completes normally.

### TC-7.7 Installing the same bundle again
**Steps:** Re-install the bundle currently running (same version).
**Pass criteria:** Installation succeeds and writes into the inactive slot (or is skipped when `install-check` hooks are added); system stays bootable. Record the observed behavior.

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
| TC-3.1 | Backend `get-current` | | |
| TC-3.2 | Backend `get-primary` | | |
| TC-3.3 | Backend `get-state` | | |
| TC-3.4 | Backend `set-primary` | | |
| TC-3.5 | Backend `set-state good` (running) | | |
| TC-3.6 | Backend `set-state good` (other) | | |
| TC-3.7 | Backend `set-state bad` | | |
| TC-3.8 | Backend invalid input | | |
| TC-4.1 | Bundle builds | | |
| TC-4.2 | Bundle metadata | | |
| TC-4.3 | Bundle uses matching rootfs | | |
| TC-5.1 | Bundle verification on target | | |
| TC-5.2 | Install to inactive slot | | |
| TC-5.3 | Reboot into new slot | | |
| TC-5.4 | Slot marked good | | |
| TC-5.5 | Update back (B → A) | | |
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

**Overall acceptance:** The integration is considered successful when all tests in sections 1–5 pass, TC-6.1 – TC-6.5 pass, and TC-6.6 demonstrates a fallback to the previous slot. Any FAIL must be recorded with logs (`journalctl -u rauc`, `nvbootctrl dump-slots-info`, serial console output).
