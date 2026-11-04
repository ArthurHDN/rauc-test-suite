*** Settings ***
Documentation       Section 2 - RAUC service and status.

Resource            ../resources/common.resource

Test Tags           section-2    readonly


*** Test Cases ***
TC-2.1 RAUC Service Running
    [Documentation]    The RAUC D-Bus service starts and is reachable.
    Run On Target    systemctl status rauc.service --no-pager    expected_rc=any
    Run On Target    systemctl is-enabled rauc.service    expected_rc=any
    # The service may be D-Bus activated: a client call starts it.
    Run On Target    rauc status
    ${r}=    Run On Target    systemctl is-active rauc.service    expected_rc=any
    Should Be Equal    ${r.stdout}    active    rauc.service is not running
    ${r}=    Run On Target    journalctl -u rauc -b --no-pager | tail -n 30
    ${errors}=    Run On Target    journalctl -u rauc -b --no-pager -p err    expected_rc=any
    ${bad}=    Get Lines Matching Regexp    ${errors.stdout}    (?i)(backend|keyring|system\\.conf)    partial_match=True
    Should Be Empty    ${bad}    RAUC reports errors about backend, keyring or system.conf:\n${bad}

TC-2.2 Rauc Status Reports Both Slots
    [Documentation]    RAUC correctly maps both slots and the booted slot.
    Run On Target    rauc status
    ${r}=    Run On Target    rauc status --output-format=json-pretty
    Save Artifact    rauc-status.json    ${r.stdout}
    ${rs}=    Get Rauc Status
    ${booted}=    Get Booted Bootname
    ${other}=    Other Bootname    ${booted}
    Should Be Equal    ${rs}[compatible]    ${MACHINE}
    Should Be Equal    ${rs}[booted_bootname]    ${booted}    RAUC booted slot differs from the booted slot reported by the device
    FOR    ${name}    ${bootname}    IN    rootfs.0    A    rootfs.1    B
        Should Be Equal    ${rs}[slots][${name}][bootname]    ${bootname}
        Should Be Equal    ${rs}[slots][${name}][class]    rootfs
        Should Be Equal    ${rs}[slots][${name}][type]    ext4
    END
    ${booted_slot}=    Bootname To Rauc Slot    ${booted}
    ${other_slot}=    Bootname To Rauc Slot    ${other}
    Should Be Equal    ${rs}[slots][${booted_slot}][state]    booted
    Should Be Equal    ${rs}[slots][${booted_slot}][boot_status]    good
    Should Be Equal    ${rs}[slots][${other_slot}][state]    inactive
    Should Be Equal    ${rs}[primary_bootname]    ${booted}    Activated slot is not the booted slot

TC-2.3 Booted Slot Matches Mounted Rootfs
    [Documentation]    RAUC's view of the booted slot equals the real root device.
    Run On Target    findmnt -no SOURCE /    expected_rc=any
    ${dev_a}=    Get Slot Device    A
    ${dev_b}=    Get Slot Device    B
    Run On Target    readlink -f ${dev_a}
    Run On Target    readlink -f ${dev_b}
    ${booted}=    Get Booted Bootname
    Root Should Be Slot    ${booted}
    ${rs}=    Get Rauc Status
    Should Be Equal    ${rs}[booted_bootname]    ${booted}

TC-2.4 Kernel Delivered With The Rootfs
    [Documentation]    The kernel (and, where the bootloader loads it from the rootfs, the DTB) and the modules of
    ...    the running kernel are in the booted rootfs, so a RAUC update replaces them together. What is
    ...    checked exactly is defined by the device plugin (check_boot_files).
    Boot Files Should Match Running Kernel
