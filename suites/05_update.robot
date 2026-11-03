*** Settings ***
Documentation       Section 5 - Update installation (A -> B -> A).
...                 The tests form one flow and must run in order; once a step fails the
...                 remaining steps are skipped because they depend on the previous state.

Resource            ../resources/common.resource

Suite Setup         Prepare Update Flow
Test Setup          Require Update Flow
Test Teardown       Update Flow Teardown
Test Tags           section-5    install    reboot


*** Variables ***
${FLOW_OK}          ${True}
${SLOT_X}           ${EMPTY}
${SLOT_Y}           ${EMPTY}
${MARKER_FILE}      /root/rauc-test-persistence-marker


*** Test Cases ***
TC-5.1 Bundle Verification On Target
    [Documentation]    The target verifies the bundle signature against /etc/rauc/ca.cert.pem.
    ${remote}=    Upload Bundle
    ${r}=    Run On Target    rauc info ${remote}    timeout=300
    ${info}=    Parse Rauc Info    ${r.stdout}
    Should Be Equal    ${info}[compatible]    ${MACHINE}
    Save Artifact    bundle-info.target.txt    ${r.stdout}

TC-5.2 Install To The Inactive Slot
    [Documentation]    Only the inactive slot is written and becomes the primary slot.
    ${sums_before}=    Run On Target    sha256sum /etc/os-release /usr/bin/rauc /usr/sbin/nvbootctrl
    ${inactive}=    Install To Inactive Slot And Check
    Should Be Equal    ${inactive}    ${SLOT_Y}
    ${sums_after}=    Run On Target    sha256sum /etc/os-release /usr/bin/rauc /usr/sbin/nvbootctrl
    Should Be Equal    ${sums_after.stdout}    ${sums_before.stdout}    Running system changed during the update
    ${rs}=    Get Rauc Status
    ${slot}=    Bootname To Rauc Slot    ${SLOT_Y}
    Should Not Be Equal    ${rs}[slots][${slot}][boot_status]    bad

TC-5.3 Reboot Into The New Slot
    Reboot And Verify Slot    ${SLOT_Y}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[current]    ${SLOT_Y}
    IF    $NEW_BUILD_MARKER_CMD and $NEW_BUILD_MARKER_REGEX
        ${r}=    Run On Target    ${NEW_BUILD_MARKER_CMD}
        Should Match Regexp    ${r.stdout}    ${NEW_BUILD_MARKER_REGEX}    Marker of the new build not found
    END

TC-5.4 Slot Marked Good After Successful Boot
    [Documentation]    nv_update_verifier / rauc-mark-good confirm the new slot.
    Wait Until Slot Is Good    ${SLOT_Y}
    # Without nv_update_verifier 'nvbootctrl verify' never runs and the bootloader retry counters
    # are never cleared (slots fail over, eventually unbootable).
    ${v}=    Run On Target    systemctl show nv_update_verifier.service -p LoadState -p Result -p ExecMainStatus
    Should Contain    ${v.stdout}    LoadState=loaded    nv_update_verifier.service is not installed
    Should Contain    ${v.stdout}    Result=success    nv_update_verifier.service did not succeed
    Should Contain    ${v.stdout}    ExecMainStatus=0    nv_update_verifier.service exited non-zero
    Run On Target    systemctl status rauc-mark-good.service --no-pager    expected_rc=any
    Run On Target    nvbootctrl dump-slots-info
    Boot Chain Should Be Healthy
    Run On Target    rauc status
    ${failed}=    Run On Target    systemctl --failed --no-legend --no-pager    expected_rc=any
    ${bad}=    Get Lines Matching Regexp    ${failed.stdout}    (?i)(rauc|nv_update_verifier)    partial_match=True
    Should Be Empty    ${bad}    Failed RAUC related units:\n${bad}

TC-5.5 Second Update Back To The Original Slot
    [Documentation]    Proves the mechanism works in both directions (B -> A).
    ${inactive}=    Install To Inactive Slot And Check
    Should Be Equal    ${inactive}    ${SLOT_X}
    Reboot And Verify Slot    ${SLOT_X}
    Wait Until Slot Is Good    ${SLOT_X}

TC-5.6 Persistence Across Update
    [Documentation]    Files in /root live in the rootfs and are therefore replaced by an update.
    ...    N/A when PERSISTENT_DATA is set (a data partition/overlay exists).
    Skip If    ${PERSISTENT_DATA}    A persistent data partition is configured
    ${r}=    Run On Target    test -e ${MARKER_FILE}    expected_rc=any
    Should Not Be Equal As Integers    ${r.rc}    0
    ...    Marker created before the update survived; expected it to be replaced together with the rootfs


*** Keywords ***
Prepare Update Flow
    Require Bundle
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    Set Suite Variable    ${SLOT_X}    ${x}
    Set Suite Variable    ${SLOT_Y}    ${y}
    Set Suite Variable    ${FLOW_OK}    ${True}
    ${id}=    Evaluate    str(uuid.uuid4())
    Run On Target    echo ${id} > ${MARKER_FILE}; sync
    Run On Target    cat ${MARKER_FILE}
    Run On Target    df -h ${TARGET_TMP}

Require Update Flow
    Skip If    not ${FLOW_OK}    A previous step of the update flow failed

Update Flow Teardown
    [Documentation]    Unlike other teardowns the primary slot must NOT be restored here:
    ...    the flow relies on the slot activated by the preceding install.
    IF    $TEST_STATUS == 'FAIL'
        Set Suite Variable    ${FLOW_OK}    ${False}
    END
    Collect Diagnostics On Failure
