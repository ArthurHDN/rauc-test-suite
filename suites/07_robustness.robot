*** Settings ***
Documentation       Section 7 - Robustness and system integration.

Resource            ../resources/common.resource

Test Tags           section-7
Test Teardown       Cleanup Robustness Test


*** Keywords ***
Cleanup Robustness Test
    Collect Diagnostics On Failure
    Run Keyword And Ignore Error    Run On Target    rm -f ${TARGET_TMP}/rauc-test-*.log
    Run Keyword And Ignore Error    Restore Boot Configuration

Kernel Log Should Be Clean
    [Documentation]    No filesystem / storage errors; the pattern comes from the device profile.
    ${r}=    Run On Target    dmesg | grep -E '${PROFILE}[kernel_error_regex]'    expected_rc=any
    Should Be Equal As Integers    ${r.rc}    1    Kernel log contains filesystem/storage errors:\n${r.stdout}

Inactive Slot Should Not Be Mounted
    [Arguments]    ${inactive}
    ${part}=    Get Slot Block Name    ${inactive}
    ${r}=    Run On Target    grep -c "/dev/${part} " /proc/mounts    expected_rc=any
    Should Be Equal As Integers    ${r.stdout}    0    Inactive slot ${inactive} is mounted

Image Written To Slot Should Match Bundle
    [Documentation]    SHA-256 over the first <image size> bytes of the slot equals the checksum in the bundle.
    [Arguments]    ${bootname}    ${bundle_info}
    ${image}=    Set Variable    ${bundle_info}[images][0]
    Should Be True    $image['size'] is not None    Image size could not be parsed from 'rauc info'
    Should Be True    $image['sha256'] is not None    Image checksum could not be parsed from 'rauc info'
    ${dev}=    Get Slot Device    ${bootname}
    # BusyBox head has no -c; dd with iflag=count_bytes is not available either, so read whole
    # MiB blocks and the remainder separately.
    ${blocks}=    Evaluate    int($image['size']) // 1048576
    ${rest}=    Evaluate    int($image['size']) % 1048576
    ${r}=    Run On Target
    ...    ( dd if=${dev} bs=1048576 count=${blocks} 2>/dev/null; dd if=${dev} bs=1048576 skip=${blocks} count=1 2>/dev/null | dd bs=1 count=${rest} 2>/dev/null ) | sha256sum
    ...    timeout=900
    ${digest}=    Evaluate    $r.stdout.split()[0]
    Should Be Equal    ${digest}    ${image}[sha256]    Slot ${bootname} does not contain the bundle image


*** Test Cases ***
TC-7.1 Repeated Update Cycles
    [Documentation]    Alternating updates (default 6: A>B>A>B>A>B), each followed by a reboot and checks.
    [Tags]    soak    install    reboot
    Require Bundle
    ${end}=    Evaluate    int(${SOAK_CYCLES}) + 1
    FOR    ${cycle}    IN RANGE    1    ${end}
        ${booted}=    Get Booted Bootname
        ${expected}=    Other Bootname    ${booted}
        Log    Update cycle ${cycle}/${SOAK_CYCLES}: ${booted} -> ${expected}    console=True
        ${inactive}=    Install To Inactive Slot And Check
        Should Be Equal    ${inactive}    ${expected}
        Reboot And Verify Slot    ${expected}
        Wait Until Slot Is Good    ${expected}
        Kernel Log Should Be Clean
        ${other}=    Other Bootname    ${expected}
        Inactive Slot Should Not Be Mounted    ${other}
        ${r}=    Run On Target    rauc status --output-format=json-pretty
        Save Artifact    cycle-${cycle}-rauc-status.json    ${r.stdout}
    END

TC-7.2 Filesystem Health Of Both Slots
    [Tags]    readonly
    Run On Target    dmesg | grep -iE '${PROFILE}[storage_log_regex]' | tail    expected_rc=any
    Kernel Log Should Be Clean
    ${has_fsck}=    Target Has Command    e2fsck
    Skip If    not ${has_fsck}    e2fsck is not installed in the image; only the kernel log was checked
    ${inactive}=    Get Inactive Bootname
    ${dev}=    Get Slot Device    ${inactive}
    Run On Target    e2fsck -fn ${dev}    timeout=900

TC-7.3 Rootfs Content Identical To Bundle Image
    [Documentation]    The image RAUC wrote to the inactive slot is bit-identical to the bundle's image.
    [Tags]    install
    ${info}=    Get Bundle Info
    ${inactive}=    Install To Inactive Slot And Check
    Image Written To Slot Should Match Bundle    ${inactive}    ${info}

TC-7.4 Bootloader Slot State Consistency
    [Tags]    readonly
    ${bl}=    Get Bootloader State
    ${rs}=    Get Rauc Status
    Should Be Equal    ${bl}[current]    ${rs}[booted_bootname]    Current bootloader slot vs RAUC booted slot
    Should Be Equal    ${bl}[active]    ${rs}[primary_bootname]    Active bootloader slot vs RAUC primary slot
    FOR    ${bootname}    IN    A    B
        Should Not Be Equal    ${bl}[slots][${bootname}]    unbootable    Slot ${bootname} is unbootable
    END
    Boot Chain Should Be Healthy

TC-7.5 No Service Regressions
    [Tags]    readonly
    ${failed}=    Run On Target    systemctl --failed --no-legend --no-pager    expected_rc=any
    Should Be Empty    ${failed.stdout}    Failed units:\n${failed.stdout}
    ${errors}=    Run On Target    journalctl -b -p err --no-pager    expected_rc=any
    ${rauc_errors}=    Get Lines Matching Regexp    ${errors.stdout}    (?i)rauc    partial_match=True
    Should Be Empty    ${rauc_errors}    RAUC related errors in the journal:\n${rauc_errors}
    Run On Target    ip a
    ${addr}=    Run On Target    ip -o addr show scope global    expected_rc=any
    Should Not Be Empty    ${addr.stdout}    No global IP address configured
    ${nic_regex}=    Set Variable    ${PROFILE}[nic_module_regex]
    IF    $nic_regex
        ${nic}=    Run On Target    lsmod | grep -E '${nic_regex}'    expected_rc=any
        IF    $nic.rc != 0
            Log    NIC module (${nic_regex}) is not loaded (may be built in or a different NIC is used)    WARN
        END
    END

TC-7.6 Concurrent Install Protection
    [Tags]    install
    ${remote}=    Upload Bundle
    ${log}=    Set Variable    ${TARGET_TMP}/rauc-test-first-install.log
    Run On Target    nohup rauc install ${remote} > ${log} 2>&1 < /dev/null &
    Wait Until Keyword Succeeds    60 s    1 s    Install Log Should Not Be Empty    ${log}
    ${second}=    Run On Target    rauc install ${remote}    expected_rc=any    timeout=120
    Should Not Be Equal As Integers    ${second.rc}    0    A second concurrent install must be rejected
    ${out}=    Combined Output    ${second}
    Should Match Regexp    ${out}    (?i)(already|busy|in progress|another|running)
    Wait Until Keyword Succeeds    ${INSTALL_TIMEOUT} s    5 s    Install Log Should Show Result    ${log}
    ${r}=    Run On Target    cat ${log}
    Should Match Regexp    ${r.stdout}    (?i)succeeded    The first installation did not complete normally

TC-7.7 Installing The Same Bundle Again
    [Documentation]    Re-installing the running version succeeds and the result stays bootable.
    ...    The observed behaviour is saved in reinstall-result.txt.
    [Tags]    install    reboot
    ${booted}=    Get Booted Bootname
    ${inactive}=    Install To Inactive Slot And Check
    Save Artifact    reinstall-result.txt    Re-install of the running bundle wrote slot ${inactive} while running from ${booted}.
    Reboot And Verify Slot    ${inactive}
    Wait Until Slot Is Good    ${inactive}


*** Keywords ***
Install Log Should Not Be Empty
    [Arguments]    ${log}
    ${r}=    Run On Target    test -s ${log}    expected_rc=any
    Should Be Equal As Integers    ${r.rc}    0

Install Log Should Show Result
    [Arguments]    ${log}
    ${r}=    Run On Target    cat ${log}    expected_rc=any
    Should Match Regexp    ${r.stdout}    (?i)(succeeded|failed)
