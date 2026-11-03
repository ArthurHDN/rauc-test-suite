*** Settings ***
Documentation       Section 6 - Failure handling and rollback.
...                 Negative bundles (6.1-6.3, 6.6) are inputs (inputs/negative/*.raucb, created by
...                 tools/prepare_qa_inputs.py); the corrupted bundle is derived on the target.
...                 Power-loss and boot-failure tests change or disturb the target.

Resource            ../resources/common.resource

Test Tags           section-6
Test Teardown       Cleanup Failure Test


*** Keywords ***
Upload Test Bundle
    [Arguments]    ${local}    ${name}
    ${remote}=    Set Variable    ${TARGET_TMP}/rauc-test-${name}.raucb
    Upload File To Target    ${local}    ${remote}
    RETURN    ${remote}

Both Slots Should Be Bootable
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[slots][A]    normal    Slot A is not bootable
    Should Be Equal    ${nv}[slots][B]    normal    Slot B is not bootable

Cleanup Failure Test
    Collect Diagnostics On Failure
    Run Keyword And Ignore Error    Run On Target    rm -f ${TARGET_TMP}/rauc-test-*.raucb ${TARGET_TMP}/rauc-test-*.log
    Run Keyword And Ignore Error    Restore Primary To Booted Slot

Recover From Boot Failure Test
    [Documentation]    Re-install the good bundle into the slot that held the broken image.
    Collect Diagnostics On Failure
    Run Keyword And Ignore Error    Run On Target    rm -f ${TARGET_TMP}/rauc-test-*.raucb
    # The failed boots flagged the slot unbootable in the rootfs chain; that flag survives a reinstall.
    ${booted}=    Get Booted Bootname
    ${inactive}=    Other Bootname    ${booted}
    Run Keyword And Ignore Error    Reset Rootfs Slot Status    ${inactive}
    ${status}    ${msg}=    Run Keyword And Ignore Error    Install To Inactive Slot And Check
    Log    Restore of the inactive slot: ${status} ${msg}
    Run Keyword And Ignore Error    Restore Primary To Booted Slot
    Run Keyword And Ignore Error    Reset Rootfs Slot Status    ${inactive}
    Boot Chain Should Be Healthy


*** Test Cases ***
TC-6.1 Reject A Wrongly Signed Bundle
    [Documentation]    A bundle signed with a certificate outside the keyring is refused.
    ...    Input: negative/wrong-signature.raucb
    [Tags]    rejected-install
    ${bundle}=    Require Negative Bundle    wrong-signature
    ${remote}=    Upload Test Bundle    ${bundle}    wrong-signature
    ${before}=    Capture Device State
    Install Should Fail    ${remote}    (?i)(signature|verif|certificate|keyring|untrusted|cms|trust)
    Device State Should Be Unchanged    ${before}

TC-6.2 Reject A Bundle With Wrong Compatible String
    [Documentation]    Input: negative/wrong-compatible.raucb
    [Tags]    rejected-install
    ${bundle}=    Require Negative Bundle    wrong-compatible
    ${remote}=    Upload Test Bundle    ${bundle}    wrong-compatible
    ${before}=    Capture Device State
    Install Should Fail    ${remote}    Compatible mismatch: Expected '[^']*' but bundle manifest has '[^']*'
    Device State Should Be Unchanged    ${before}

TC-6.3 Reject A Plain Format Bundle
    [Documentation]    bundle-formats=-plain prevents the legacy format. Input: negative/plain-format.raucb
    [Tags]    rejected-install
    ${bundle}=    Require Negative Bundle    plain-format
    ${remote}=    Upload Test Bundle    ${bundle}    plain-format
    ${before}=    Capture Device State
    Install Should Fail    ${remote}    Bundle format 'plain' not allowed
    Device State Should Be Unchanged    ${before}

TC-6.4 Corrupted Bundle
    [Documentation]    A bundle with damaged payload is rejected and never activated.
    [Tags]    install
    ${remote}=    Upload Bundle
    ${corrupt}=    Set Variable    ${TARGET_TMP}/rauc-test-corrupt.raucb
    # BusyBox dd has no conv=notrunc: rebuild the file as head + 1 MiB of random data + tail (same size).
    Run On Target    dd if=${remote} of=${corrupt} bs=1M count=10 && dd if=/dev/urandom bs=1M count=1 >> ${corrupt} && dd if=${remote} bs=1M skip=11 >> ${corrupt}
    ${orig_size}=    Run On Target    wc -c < ${remote}
    ${new_size}=    Run On Target    wc -c < ${corrupt}
    Should Be Equal    ${new_size.stdout}    ${orig_size.stdout}    Corrupted copy must keep the original size
    Run On Target    cmp -s ${remote} ${corrupt}    expected_rc=1
    ${before}=    Capture Device State
    Install Should Fail    ${corrupt}    (?i)(verity|hash|signature|checksum|digest|verif|corrupt|invalid|fail|error)
    # The inactive slot may be partially written, but must not be activated.
    Boot Configuration Should Be Unchanged    ${before}

TC-6.5 Power Loss During Installation
    [Documentation]    Interrupting the write does not brick the device; the old slot still boots.
    [Tags]    install    reboot    power-loss
    ${x}=    Get Booted Bootname
    ${remote}=    Upload Bundle
    ${log}=    Set Variable    ${TARGET_TMP}/rauc-test-power-loss.log
    Run On Target    nohup rauc install ${remote} > ${log} 2>&1 < /dev/null &
    ${pct}=    Set Variable    ${-1}
    FOR    ${i}    IN RANGE    600
        ${r}=    Run On Target    cat ${log}    expected_rc=any
        Should Not Match Regexp    ${r.stdout}    (?i)succeeded    Installation finished before power was cut
        ${pct}=    Last Progress Percent    ${r.stdout}
        IF    ${pct} >= ${POWER_LOSS_PERCENT}    BREAK
        IF    ${pct} < 0 and ${i} >= 60
            Log    No progress output after 30 s, cutting power anyway    WARN
            BREAK
        END
        Sleep    0.5 s
    END
    Log    Cutting power at ${pct} % installation progress    console=True
    Save Artifact    power-loss-progress.txt    progress at reset: ${pct} %
    Reboot Target And Reconnect    sysrq    ${BOOT_TIMEOUT}
    ${booted}=    Get Booted Bootname
    Should Be Equal    ${booted}    ${x}    Device did not boot the previous slot after power loss
    Root Should Be Slot    ${x}
    Wait Until Slot Is Good    ${x}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${x}    Interrupted install must not activate the other slot
    # A subsequent install succeeds.
    ${remote}=    Upload Bundle
    Install Bundle Should Succeed    ${remote}

TC-6.6 Boot Failure Triggers Fallback
    [Documentation]    A new slot that fails to boot is not retried forever; the device falls back.
    ...    A bundle with an empty ext4 rootfs (no init) is installed (input negative/broken-rootfs.raucb).
    ...    Hung boots need a power
    ...    cycle: set JETSON_POWER_CYCLE_CMD to automate it (see README).
    [Tags]    install    reboot    boot-failure
    ${bundle}=    Require Negative Bundle    broken-rootfs
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    ${remote}=    Upload Test Bundle    ${bundle}    broken-rootfs
    Install Bundle Should Succeed    ${remote}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${y}
    ${status}    ${msg}=    Run Keyword And Ignore Error
    ...    Reboot Target And Reconnect    soft    ${BOOT_TIMEOUT}
    FOR    ${attempt}    IN RANGE    ${BOOT_FAILURE_ATTEMPTS}
        IF    '${status}' == 'PASS'    BREAK
        Log    Device not reachable yet (attempt ${attempt}): ${msg}    WARN
        ${status}    ${msg}=    Run Keyword And Ignore Error    Wait For Target    ${BOOT_TIMEOUT}
    END
    Should Be Equal    ${status}    PASS    Device never returned: ${msg}
    ${booted}=    Get Booted Bootname
    Should Be Equal    ${booted}    ${x}    Device did not fall back to the previous slot
    Wait Until Slot Is Good    ${x}
    # The bootloader chain keeps reporting 'normal'; the failed rootfs is flagged in the rootfs chain.
    ${nv}=    Get Nvbootctrl Info    rootfs
    Should Be Equal    ${nv}[slots][${y}]    unbootable    The broken slot must be flagged unbootable
    ${rs}=    Get Rauc Status
    ${slot}=    Bootname To Rauc Slot    ${y}
    Should Not Be Equal    ${rs}[slots][${slot}][boot_status]    good
    [Teardown]    Recover From Boot Failure Test

TC-6.7 Mark-Bad Path Via RAUC
    [Documentation]    `rauc status mark-bad` redirects the next boot to the other slot.
    [Tags]    reboot    slot-change
    Both Slots Should Be Bootable
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    Restore Primary To Booted Slot
    Run On Target    rauc status mark-bad
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${y}    Active slot after mark-bad
    Reboot And Verify Slot    ${y}
    Wait Until Slot Is Good    ${y}
    ${slot_x}=    Bootname To Rauc Slot    ${x}
    Run On Target    rauc status mark-active ${slot_x}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${x}    Active slot after mark-active
    ${rs}=    Get Rauc Status
    Should Be Equal    ${rs}[primary_bootname]    ${x}

TC-6.8 Manual Slot Switching Through RAUC
    [Documentation]    Switch to the other slot and back again.
    [Tags]    reboot    slot-change
    Both Slots Should Be Bootable
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    Run On Target    rauc status mark-active other
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${y}
    Reboot And Verify Slot    ${y}
    Wait Until Slot Is Good    ${y}
    ${rs}=    Get Rauc Status
    Should Be Equal    ${rs}[primary_bootname]    ${y}
    # Restore the original slot.
    Run On Target    rauc status mark-active other
    Reboot And Verify Slot    ${x}
    Wait Until Slot Is Good    ${x}
