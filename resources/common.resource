*** Settings ***
Documentation       Shared variables and keywords for the RAUC integration test suites.
...
...                 Connection settings (JETSON_HOST, JETSON_USER, JETSON_PASSWORD, ...) are read
...                 by the JetsonTarget library from the environment or the env file and are never
...                 passed through Robot variables, so secrets cannot leak into the logs.

Library             OperatingSystem
Library             String
Library             Collections
Library             JetsonTarget
Library             RaucHelpers
Library             TestInputs


*** Variables ***
${MACHINE}                      jetson-orin-nano-devkit-nvme

# All files the tests need come from this directory (see libraries/TestInputs.py for the layout).
${INPUTS_DIR}                   ${{ os.path.normpath(os.path.join(r'${CURDIR}', '..', 'inputs')) }}
# Bundle under test. Empty = newest *.raucb in ${INPUTS_DIR}/bundle.
${BUNDLE_PATH}                  ${EMPTY}
# How *.raucb inputs are preserved in <results>/inputs/test-inputs: link | copy | none
${BUNDLE_ARCHIVE_MODE}          link

# Filled by Initialise Test Run.
${INPUTS}                       ${EMPTY}
${BUNDLE}                       ${EMPTY}
${INITIAL_BOOTED}               ${EMPTY}

${BACKEND}                      /usr/libexec/rauc-nvbootctrl-backend
# GUID of the UEFI variables RootfsStatusSlotA / RootfsStatusSlotB.
${NVIDIA_GUID}                  781e084c-a330-417c-b678-38e696380cb9
# Mount point of the persistent DATA partition (DATA_MOUNT_POINT in meta-jetson-rauc).
${DATA_MOUNT_POINT}             /data
${TARGET_TMP}                   /tmp
${BOOT_TIMEOUT}                 900
${INSTALL_TIMEOUT}              1800
${SOAK_CYCLES}                  6
${POWER_LOSS_PERCENT}           40
${BOOT_FAILURE_ATTEMPTS}        5
# Optional "was the new build installed?" check after an update:
# run NEW_BUILD_MARKER_CMD on the target and match its output against NEW_BUILD_MARKER_REGEX.
${NEW_BUILD_MARKER_CMD}         ${EMPTY}
${NEW_BUILD_MARKER_REGEX}       ${EMPTY}
# Set to True when a persistent data partition exists (TC-5.6 then becomes N/A).
${PERSISTENT_DATA}              ${False}


*** Keywords ***
# ----------------------------------------------------------------- run set-up
Initialise Test Run
    [Documentation]    Connect, resolve + archive the input files and record the initial target state.
    Connect To Target
    ${settings}=    Get Target Settings
    Log    Target: ${settings}
    Resolve And Archive Inputs
    Record Target State    initial
    ${booted}=    Get Booted Bootname
    Set Global Variable    ${INITIAL_BOOTED}    ${booted}
    Log    Initial booted slot: ${booted}    console=True

Finalise Test Run
    [Documentation]    Leave the device with its primary slot equal to the booted slot and record the end state.
    Run Keyword And Ignore Error    Restore Primary To Booted Slot
    Run Keyword And Ignore Error    Record Target State    final
    Disconnect From Target

Resolve And Archive Inputs
    [Documentation]    Find the supplied input files and keep a copy/hardlink + checksums in <results>/inputs/test-inputs.
    ${inputs}=    Discover Inputs    ${INPUTS_DIR}    ${BUNDLE_PATH}
    Set Global Variable    ${INPUTS}    ${inputs}
    Set Global Variable    ${BUNDLE}    ${inputs}[bundle]
    IF    not $inputs['dir']
        Log    Inputs directory '${INPUTS_DIR}' not found; only tests without input files can run.    WARN
    END
    IF    not $BUNDLE
        Log    No RAUC bundle found (--bundle '${BUNDLE_PATH}', ${INPUTS_DIR}/bundle). Bundle dependent tests will fail.    WARN
    END
    ${manifest}=    Archive Inputs    ${inputs}    ${OUTPUT DIR}/inputs/test-inputs    ${BUNDLE_ARCHIVE_MODE}
    ${files}=    Set Variable    ${manifest}[files]
    FOR    ${entry}    IN    @{files}
        Log    Input ${entry}[role]: ${entry}[source] (sha256 ${entry}[sha256])    console=True
    END

Record Target State
    [Documentation]    Store identifying information about the target in the current artifact directory.
    [Arguments]    ${label}
    @{commands}=    Create List
    ...    date -u
    ...    uname -a
    ...    cat /etc/os-release
    ...    cat /proc/cmdline
    ...    cat /proc/sys/kernel/random/boot_id
    ...    rauc --version
    ...    rauc status
    ...    nvbootctrl dump-slots-info
    ...    ls -l /dev/disk/by-partlabel/
    ...    cat /etc/rauc/system.conf
    ...    sha256sum /etc/rauc/system.conf /etc/rauc/ca.cert.pem ${BACKEND}
    FOR    ${cmd}    IN    @{commands}
        Run On Target    ${cmd}    timeout=60    expected_rc=any
    END
    Log    Recorded target state '${label}'

Require Bundle
    IF    not $BUNDLE
        Fail    No bundle available. Put a *.raucb into ${INPUTS_DIR}/bundle or pass --bundle <file>.
    END

Require Input
    [Documentation]    Skip the test when an optional input file was not supplied. ${key} is a key of ${INPUTS}.
    [Arguments]    ${key}    ${description}
    Skip If    not $INPUTS[$key]    Input not supplied: ${description} (inputs directory: ${INPUTS_DIR})

Require Negative Bundle
    [Documentation]    Skip unless negative/${name}.raucb was supplied; returns its path.
    [Arguments]    ${name}
    ${path}=    Evaluate    $INPUTS['negative'].get($name, '')
    Skip If    not $path    Input not supplied: negative/${name}.raucb (inputs directory: ${INPUTS_DIR})
    RETURN    ${path}

# ------------------------------------------------------------- slot state
Get Rauc Status
    ${r}=    Run On Target    rauc status --output-format=json    timeout=60
    ${status}=    Parse Rauc Status    ${r.stdout}
    RETURN    ${status}

Get Nvbootctrl Info
    [Documentation]    Parsed 'nvbootctrl dump-slots-info' of the bootloader chain (default) or of the
    ...    rootfs chain (${chain}=rootfs). A rootfs that failed to boot is only flagged
    ...    unbootable in the rootfs chain; the bootloader chain keeps reporting 'normal'.
    [Arguments]    ${chain}=bootloader
    ${r}=    Run On Target    nvbootctrl -t ${chain} dump-slots-info    timeout=60
    ${info}=    Parse Nvbootctrl Slots    ${r.stdout}
    RETURN    ${info}

Reset Rootfs Slot Status
    [Documentation]    Clear the 'unbootable' flag of a slot in the rootfs chain. Neither nvbootctrl
    ...    set-active-boot-slot nor a reboot clears it, so the UEFI variable is reset (0 = normal).
    [Arguments]    ${bootname}
    ${nv}=    Get Nvbootctrl Info    rootfs
    IF    $nv['slots'][$bootname] != 'unbootable'    RETURN
    Log    Resetting rootfs status of slot ${bootname}    console=True
    Run On Target
    ...    printf '\\000\\000\\000\\000' > ${TARGET_TMP}/rauc-test-zero.bin && efivar -n ${NVIDIA_GUID}-RootfsStatusSlot${bootname} -w -f ${TARGET_TMP}/rauc-test-zero.bin -A 7 && rm -f ${TARGET_TMP}/rauc-test-zero.bin
    ...    timeout=60
    ${nv}=    Get Nvbootctrl Info    rootfs
    Should Be Equal    ${nv}[slots][${bootname}]    normal    Rootfs status of slot ${bootname} could not be reset

Boot Chain Should Be Healthy
    [Documentation]    Neither the bootloader nor the rootfs slot chain may contain an unbootable slot.
    ...    The bootloader fails slots over (and finally boots the network-less recovery kernel) when
    ...    'nvbootctrl verify' is not run, so the rootfs state is checked as well and saved as evidence.
    FOR    ${type}    IN    bootloader    rootfs
        ${r}=    Run On Target    nvbootctrl -t ${type} dump-slots-info    timeout=60    expected_rc=any
        ${stamp}=    Evaluate    __import__('time').strftime('%H%M%S')
        Save Artifact    nvbootctrl-${type}-${stamp}.txt    ${r.stdout}\n${r.stderr}
        ${bad}=    Get Lines Matching Regexp    ${r.stdout}    (?i)^slot:.*status:\\s*unbootable    partial_match=True
        Should Be Empty    ${bad}    Unbootable ${type} slot:\n${r.stdout}
    END

Boot Files Should Match Running Kernel
    [Documentation]    The booted rootfs carries the kernel, initrd and DTB that L4TLauncher loads through
    ...    extlinux.conf, so they are replaced together with the rootfs by an update. The kernel DTB comes
    ...    from the rootfs only when extlinux.conf has an FDT line (UBOOT_EXTLINUX_FDT).
    ${conf}=    Run On Target    cat /boot/extlinux/extlinux.conf
    Should Match Regexp    ${conf.stdout}    (?m)^\\s*LINUX\\s+/boot/Image\\s*$
    ${fdt}=    Get Lines Matching Regexp    ${conf.stdout}    \\s*FDT\\s+\\S+\\.dtb\\s*
    Should Not Be Empty    ${fdt}    extlinux.conf has no FDT line; the kernel DTB is not delivered with the rootfs
    ${dtb}=    Evaluate    $fdt.split()[1]
    Run On Target    test -s ${dtb}
    ${uname}=    Run On Target    uname -r
    Run On Target    test -s /boot/Image && grep -c -- "${uname.stdout}" /boot/Image    timeout=120
    Run On Target    test -d /lib/modules/${uname.stdout}

Get Booted Bootname
    [Documentation]    Bootname (A/B) of the running slot according to nvbootctrl.
    ${r}=    Run On Target    nvbootctrl get-current-slot    timeout=60
    ${name}=    Index To Bootname    ${r.stdout}
    RETURN    ${name}

Get Inactive Bootname
    ${booted}=    Get Booted Bootname
    ${other}=    Other Bootname    ${booted}
    RETURN    ${other}

Get Root Block Name
    [Documentation]    Kernel name (e.g. nvme0n1p1) of the block device mounted on /.
    ${r}=    Run On Target
    ...    basename "$(readlink -f /sys/dev/block/$(awk '$5=="/"{m=$3} END{print m}' /proc/self/mountinfo))"
    RETURN    ${r.stdout}

Get Partlabel Block Name
    [Documentation]    Kernel name of the partition behind /dev/disk/by-partlabel for a slot.
    [Arguments]    ${bootname}
    ${label}=    Bootname To Partlabel    ${bootname}
    ${r}=    Run On Target    basename "$(readlink -f /dev/disk/by-partlabel/${label})"
    RETURN    ${r.stdout}

Root Should Be Slot
    [Arguments]    ${bootname}
    ${root}=    Get Root Block Name
    ${expected}=    Get Partlabel Block Name    ${bootname}
    Should Be Equal    ${root}    ${expected}    Root filesystem is not the partition of slot ${bootname}

Restore Primary To Booted Slot
    [Documentation]    Make the bootloader boot the currently running slot again.
    ${booted}=    Get Booted Bootname
    Run On Target    ${BACKEND} set-primary ${booted}    expected_rc=any
    ${info}=    Get Nvbootctrl Info
    Should Be Equal    ${info}[active]    ${booted}

Capture Device State
    [Documentation]    Snapshot of bootloader + RAUC slot state and the head of the inactive partition.
    ${nv}=    Get Nvbootctrl Info
    ${rs}=    Get Rauc Status
    ${inactive}=    Other Bootname    ${nv}[current]
    ${label}=    Bootname To Partlabel    ${inactive}
    ${head}=    Run On Target
    ...    dd if=/dev/disk/by-partlabel/${label} bs=1M count=4 2>/dev/null | sha256sum
    ...    timeout=120
    &{state}=    Create Dictionary
    ...    nv_current=${nv}[current]
    ...    nv_active=${nv}[active]
    ...    nv_status_A=${nv}[slots][A]
    ...    nv_status_B=${nv}[slots][B]
    ...    rauc_booted=${rs}[booted_bootname]
    ...    rauc_primary=${rs}[primary_bootname]
    ...    inactive_head=${head.stdout}
    RETURN    ${state}

Boot Configuration Should Be Unchanged
    [Documentation]    Active/current slots and slot statuses equal the captured snapshot.
    [Arguments]    ${before}
    ${after}=    Capture Device State
    FOR    ${key}    IN    nv_current    nv_active    nv_status_A    nv_status_B    rauc_booted    rauc_primary
        Should Be Equal    ${after}[${key}]    ${before}[${key}]    ${key} changed
    END
    RETURN    ${after}

Device State Should Be Unchanged
    [Documentation]    Boot configuration AND the first MiB of the inactive partition are unchanged.
    [Arguments]    ${before}
    ${after}=    Boot Configuration Should Be Unchanged    ${before}
    Should Be Equal    ${after}[inactive_head]    ${before}[inactive_head]    Inactive slot was modified

Slot Should Be Good
    [Arguments]    ${bootname}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[slots][${bootname}]    normal    nvbootctrl status of slot ${bootname}
    ${rs}=    Get Rauc Status
    ${slot_name}=    Bootname To Rauc Slot    ${bootname}
    Should Be Equal    ${rs}[slots][${slot_name}][boot_status]    good

Wait Until Slot Is Good
    [Arguments]    ${bootname}    ${timeout}=300
    Log    Waiting up to ${timeout} s for slot ${bootname} to be marked good (polling every 10 s)    console=True
    ${status}    ${msg}=    Run Keyword And Ignore Error
    ...    Wait Until Keyword Succeeds    ${timeout}    10 s    Slot Should Be Good    ${bootname}
    IF    '${status}' == 'FAIL'
        Log    TIMEOUT: slot ${bootname} was not marked good within ${timeout} s    level=WARN
        Fail    ${msg}
    END

# ----------------------------------------------------------- bundle handling
Upload Bundle
    [Documentation]    Copy the bundle to the target (skipped if an identical file is already there).
    ...    Returns the path on the target.
    Require Bundle
    ${name}=    Evaluate    os.path.basename(r'''${BUNDLE}''')
    ${remote}=    Set Variable    ${TARGET_TMP}/${name}
    ${local_sha}=    File Sha256    ${BUNDLE}
    ${r}=    Run On Target    test -f ${remote} && sha256sum ${remote} | cut -d' ' -f1    timeout=600    expected_rc=any
    IF    $r.stdout == $local_sha
        Log    Bundle already present on target with matching checksum
        RETURN    ${remote}
    END
    ${size}=    Get File Size    ${BUNDLE}
    ${avail}=    Run On Target    df -Pk ${TARGET_TMP} | awk 'NR==2{print $4}'
    ${need_kb}=    Evaluate    int(${size} / 1024 * 1.05)
    Should Be True    int(${avail.stdout}) > ${need_kb}
    ...    Not enough space in ${TARGET_TMP}: ${avail.stdout} KiB free, ${need_kb} KiB required
    Upload File To Target    ${BUNDLE}    ${remote}
    RETURN    ${remote}

Install Bundle
    [Documentation]    rauc install of a bundle on the target; returns the command result.
    [Arguments]    ${remote}    ${expected_rc}=0
    ${r}=    Run On Target    rauc install ${remote}    timeout=${INSTALL_TIMEOUT}    expected_rc=${expected_rc}
    RETURN    ${r}

Install Bundle Should Succeed
    [Arguments]    ${remote}
    ${r}=    Install Bundle    ${remote}
    ${out}=    Combined Output    ${r}
    Should Match Regexp    ${out}    (?i)succeeded
    RETURN    ${r}

Install Should Fail
    [Documentation]    rauc install must exit non-zero and print a message matching ${regex}.
    [Arguments]    ${remote}    ${regex}
    ${r}=    Install Bundle    ${remote}    expected_rc=any
    Should Not Be Equal As Integers    ${r.rc}    0    rauc install unexpectedly succeeded
    ${out}=    Combined Output    ${r}
    Should Match Regexp    ${out}    ${regex}    Unexpected rauc error message
    RETURN    ${r}

Install To Inactive Slot And Check
    [Documentation]    Upload + install; verifies the bootloader now points to the other slot.
    ${booted}=    Get Booted Bootname
    ${inactive}=    Other Bootname    ${booted}
    ${remote}=    Upload Bundle
    Install Bundle Should Succeed    ${remote}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${inactive}    Active bootloader slot after install
    Should Be Equal    ${nv}[current]    ${booted}    Current slot must not change during install
    ${rs}=    Get Rauc Status
    Should Be Equal    ${rs}[primary_bootname]    ${inactive}    RAUC primary after install
    Should Be Equal    ${rs}[booted_bootname]    ${booted}
    RETURN    ${inactive}

Reboot And Verify Slot
    [Arguments]    ${expected}    ${method}=soft
    # Safety guard: rebooting with an unbootable slot can end in the recovery kernel (no network).
    Boot Chain Should Be Healthy
    ${marker}=    Write Data Marker
    ${seconds}=    Reboot Target And Reconnect    ${method}    ${BOOT_TIMEOUT}
    Data Marker Should Be Intact    ${marker}
    Log    Target rebooted in ${seconds} s    console=True
    ${booted}=    Get Booted Bootname
    Should Be Equal    ${booted}    ${expected}    Booted from the wrong slot
    ${rs}=    Get Rauc Status
    Should Be Equal    ${rs}[booted_bootname]    ${expected}
    Root Should Be Slot    ${expected}
    Boot Files Should Match Running Kernel

Write Data Marker
    [Documentation]    Write a unique token to the persistent /data partition (it must survive reboots and
    ...    slot switches). Returns the token.
    ${token}=    Evaluate    __import__('uuid').uuid4().hex
    Run On Target    echo ${token} > /data/.rauc-tests-marker && sync
    RETURN    ${token}

Data Marker Should Be Intact
    [Arguments]    ${token}
    ${r}=    Run On Target    cat /data/.rauc-tests-marker
    Should Be Equal    ${r.stdout}    ${token}    /data lost its content across the reboot

Get Bundle Info
    [Documentation]    rauc info for the bundle, run on the target (so the signature is checked against
    ...    the target's own keyring). Returns the parsed dictionary.
    Require Bundle
    ${remote}=    Upload Bundle
    ${r}=    Run On Target    rauc info ${remote}    timeout=300
    ${out}=    Combined Output    ${r}
    Save Artifact    bundle-info.txt    ${out}
    ${info}=    Parse Rauc Info    ${r.stdout}
    RETURN    ${info}

# ------------------------------------------------------------------ teardown
Collect Diagnostics
    [Documentation]    Evidence for failed tests: slot state, rauc journal, kernel log, failed units.
    @{commands}=    Create List
    ...    rauc status
    ...    nvbootctrl dump-slots-info
    ...    systemctl --failed --no-pager
    ...    journalctl -u rauc -b --no-pager | tail -n 200
    ...    dmesg | tail -n 200
    FOR    ${cmd}    IN    @{commands}
        Run On Target    ${cmd}    timeout=60    expected_rc=any
    END

Collect Diagnostics On Failure
    IF    $TEST_STATUS == 'FAIL'
        Run Keyword And Ignore Error    Collect Diagnostics
    END

Restore Slot State And Collect Diagnostics
    [Documentation]    Test teardown for tests that change the bootloader state.
    Collect Diagnostics On Failure
    Run Keyword And Ignore Error    Restore Primary To Booted Slot
