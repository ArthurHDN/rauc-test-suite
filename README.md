# RAUC integration tests

Automated Robot Framework implementation of `RAUC-TEST-PLAN.md` (in this repository, so it is part of the QA
package). Every test case `TC-x.y` of the plan is one Robot test; the target is reached over SSH. Every run is
stored in its own directory so each result can be audited and reproduced.

This is a standalone repository. It can be cloned anywhere (it is usually checked out next to, or inside, the Yocto
image repository, but it does not have to be) and is run from its own root directory. It has no dependency on the
image repository, its layout or its tools; the only coupling is the inputs directory (see below).

The tests are hardware independent. Everything that differs between devices (boot loader, partition layout, how
the A/B state is read) lives in one plugin per device under `libraries/devices/`. Supported devices
(`./run_tests.py --list-devices`):

| `DEVICE` (= Yocto `MACHINE` = RAUC `compatible`) | Hardware |
|---|---|
| `jetson-orin-nano-devkit-nvme` | Jetson Orin Nano devkit, NVMe |
| `jetson-orin-nano-devkit` | Jetson Orin Nano devkit, SD card |
| `raspberrypi4-64` | Raspberry Pi 4 (U-Boot) |

Device specifics and skipped tests are listed in the appendix of `RAUC-TEST-PLAN.md`. To support another device see
`DEVICES.md`. Note that a new device also needs an entry in `tools/prepare_qa_inputs.py` and a kas configuration in
the Yocto image repository (see `DEVICES.md`); those changes are made there, not in this repository.

The repository is self-contained. It does not build, sign or generate anything and does not need the Yocto
repository: everything the tests need is supplied through an **inputs directory** (see below).

## Setup

```bash
git clone <url of the rauc-test-suite repository> rauc-test-suite   # or unpack the QA package
cd rauc-test-suite
uv venv && uv pip install --python .venv/bin/python -r requirements.txt   # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp env.example .env       # then edit
```

Only Python 3.10+ and the packages in `requirements.txt` are needed on the machine that runs the tests.

## Inputs directory

Default `rauc-test-suite/inputs/`, or any folder given with `--inputs DIR`. The inputs belong to one device: use a
separate directory per device (the bundle, `expected/system.conf` and the negative bundles carry its compatible
string). All files are optional except the bundle; tests that need a missing optional file are skipped (and say
which file is missing).

```
<inputs>/
  bundle/<name>.raucb          bundle under test (newest file used); or --bundle FILE
  expected/system.conf         reference /etc/rauc/system.conf of the image (TC-1.2); the build-time placeholders are
                               substituted by the test: @RAUC_COMPATIBLE@ and @DATA_MOUNT_POINT@ (Jetson),
                               @@MACHINE@@ (Raspberry Pi)
  expected/ca.cert.pem         reference keyring certificate (TC-1.4)
  expected/rootfs.sha256       "<sha256>  <name>" of the rootfs image inside the bundle (TC-4.3)
  negative/wrong-signature.raucb   signed by an unknown certificate   (TC-6.1)
  negative/wrong-compatible.raucb  valid signature, other compatible  (TC-6.2)
  negative/plain-format.raucb      legacy plain format                (TC-6.3)
  negative/broken-rootfs.raucb     valid bundle, unbootable rootfs    (TC-6.6)
  inputs.json                  metadata and SHA-256 of every file; verified by TC-4.1
```

The inputs are not part of this repository. They are produced on the build host (where the Yocto build output and
the signing key live) with `tools/prepare_qa_inputs.py` of the **Yocto image repository** (the one containing
`kas/`, `internal/` and `build/`), run from the root of that repository:

```bash
tools/prepare_qa_inputs.py --out qa-inputs                  # newest bundle from the build output + negative bundles
tools/prepare_qa_inputs.py --out qa-inputs --build          # run the kas build first
tools/prepare_qa_inputs.py --out qa-inputs --package        # also write rauc-qa-<timestamp>.tar.gz (+ .sha256)
tools/prepare_qa_inputs.py --machine raspberrypi4-64 --out qa-inputs-rpi   # another device (default: detected)
```

The generator needs `rauc`, `openssl` and `mke2fs` (negative bundles; `--skip-negative` to omit them), and `kas`
only with `--build`. No private key is written to the output. The QA machine needs none of these tools.

The output directory can be anywhere: pass it to the suite with `--inputs DIR` (absolute path, or relative to the
current directory), or copy/symlink it to `inputs/` in this repository.

Shipping to QA: hand over the `--package` archive together with this repository (a clone or an archive of it). QA
unpacks both and runs `./run_tests.py --inputs <inputs directory>` as described below; no access to the Yocto image
repository is required.

## Target access

Settings are read from the process environment first, then from `.env` (or `--env-file`).

| Variable | Meaning |
|---|---|
| `DEVICE` | required (or `--device`): device under test, see `--list-devices` |
| `TARGET_HOST` | required, IP or hostname |
| `TARGET_USER` | default `root` |
| `TARGET_PASSWORD` | may be empty or unset (empty-password login, e.g. `debug-tweaks` images) |
| `TARGET_PORT` | default `22` |
| `TARGET_KEY_FILE`, `TARGET_KEY_PASSPHRASE` | optional key login, tried before the password |
| `TARGET_CONNECT_TIMEOUT` | SSH connect timeout in seconds, default `15` |
| `TARGET_POWER_CYCLE_CMD`, `TARGET_POWER_CYCLE_AFTER` | optional command run on the test machine to power-cycle a hung device (TC-6.6; required on the Raspberry Pi) |

The former `JETSON_*` names (and `JETSON_ENV_FILE`) are still accepted; `TARGET_*` wins if both are set.

Authentication order: key, password (including the empty one), SSH "none". The password is never passed as a
Robot variable, never put on a command line and never written to the results. `--ask-password` prompts for it
(Enter = empty). The host key is not verified; its fingerprint is logged per run.

## Running

```bash
./run_tests.py --list-devices                               # supported devices
./run_tests.py --device raspberrypi4-64 --inputs qa-inputs-rpi   # profile "safe"; DEVICE may also be set in .env
./run_tests.py --inputs /path/to/qa-inputs                  # device from .env
./run_tests.py --inputs qa-inputs --profile update
./run_tests.py --inputs qa-inputs --bundle /other/bundle.raucb   # override the bundle only
./run_tests.py --profile acceptance --label before-release
./run_tests.py --host 192.168.1.50 --user root --ask-password
./run_tests.py -v SOAK_CYCLES:2 -- -t "TC-7.*"  # options after -- go to Robot Framework
./run_tests.py --dry-run                        # parse only, no device access
```

### Profiles (`robot.toml`)

| Profile | Runs | Changes the device |
|---|---|---|
| `safe` (default) | sections 1, 2, 3, 4, 6.1-6.3, 7.2, 7.4, 7.5 | bootloader primary slot toggled and restored (section 3) |
| `update` | + installs/reboots: 5, 6.4, 6.7, 6.8, 7.3, 7.6, 7.7 | writes the inactive slot, reboots |
| `acceptance` | + power loss (6.5) and boot failure (6.6) | interrupts installs, may need power cycling |
| `full` | + soak TC-7.1 (alternating updates) | many installs/reboots |

Tags: `readonly`, `slot-change`, `rejected-install`, `install`, `reboot`, `power-loss`, `boot-failure`, `soak`,
plus `section-N`. Select with `-- -i TAG` / `-- -e TAG`. Teardowns restore the primary slot and remove temp files.

All checks of the bundle itself (`rauc info`, signature, compatible) run on the target, so no host-side `rauc`
is needed.

## Results and audit trail

```
results/<UTC timestamp>[_label]/        results/latest -> newest run
  RESULT-SHEET.md      one line per test: result, time, evidence directory, message
  manifest.json        target (no secrets), profile, git commit of this repository, inputs, bundle sha256, tool versions
  reproduce.sh         re-runs with the same arguments and the archived inputs
  SHA256SUMS           hashes of every file in the run (sha256sum -c SHA256SUMS)
  output.xml log.html report.html xunit.xml debug.log console.log   Robot Framework logs
  inputs/              test-inputs/ (bundle, expected files, negative bundles: hardlink/copy per --bundle-archive,
                       plus inputs-manifest.json with sha256), rauc-tests-sources.tar.gz, redacted env file,
                       git diff/status (when this repository is a git checkout), python packages
  audit/commands.jsonl every command run on the target with timing, rc, stdout, stderr
  audit/results.jsonl  one record per finished test
  artifacts/<TC-id>_<title>/   commands.log, test.json, SHA256SUMS and files saved by the test
                       (rauc status/info output, journals, dmesg, downloaded files, ...)
```

Failed tests additionally collect diagnostics (journal, `rauc status`, the boot loader state commands of the device) in their artifact directory.
To reproduce a run: provide the password and run `results/<run>/reproduce.sh` (set `TESTS_DIR` to an unpacked
`inputs/rauc-tests-sources.tar.gz` to use exactly the same test sources).
Use `--bundle-archive none` to skip keeping a copy of the inputs in the results (the paths and checksums are still
recorded).

## Layout

- `suites/` - `01_installation` ... `07_robustness`, plus `__init__.robot` (connection, initial-state capture, final slot restore)
- `resources/common.resource` - shared variables and keywords
- `libraries/TargetSSH.py` - SSH execution with audit logging; `DeviceUnderTest.py` - keyword library that delegates
  to the selected device plugin; `devices/` - one plugin per device (`base.py` = contract, `_template.py` = starting
  point); `TestInputs.py` - inputs discovery, verification and archiving; `RaucHelpers.py` - parsers/helpers;
  `AuditListener.py` - per-test records; `audit_common.py` - settings (with legacy aliases) and hashing
- `tests/` - unit tests of the plugins and settings against fake targets (no device needed)
- `robot.toml` - RobotCode/Robot options and profiles; `run_tests.py` - runner that builds the result directory
  (re-executes itself with `.venv/bin/python` when the current interpreter lacks the dependencies)
- `RAUC-TEST-PLAN.md` - the test plan; `DEVICES.md` - how to add a device; `env.example` - template for `.env`;
  `requirements.txt` - Python dependencies

## Unit tests

The device plugins and the settings are tested without hardware (stdlib `unittest`, fake SSH targets):

```bash
.venv/bin/python -m unittest discover -s tests
```

## Safety notes

- `update` and higher profiles overwrite the inactive rootfs slot and reboot the device; keep console/power access ready.
- TC-6.5 cuts the install with a hard (sysrq) reset at `POWER_LOSS_PERCENT` progress; TC-6.6 installs a bundle with an
  empty rootfs to force a failed boot.
- Variables can be set with `-v NAME:VALUE`: `DEVICE`, `DATA_MOUNT_POINT`, `BOOT_TIMEOUT`, `INSTALL_TIMEOUT`,
  `SOAK_CYCLES`, `POWER_LOSS_PERCENT`, `BOOT_FAILURE_ATTEMPTS`.
