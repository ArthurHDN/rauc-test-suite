*** Settings ***
Documentation       Section 3 - Bootloader interface.
...                 The interface RAUC uses to read and change the A/B boot state is exercised directly, to
...                 isolate it from the install workflow. What it is depends on the device plugin: the custom
...                 nvbootctrl backend on the Jetson, RAUC's built-in U-Boot backend (via `rauc status mark-*`)
...                 on the Raspberry Pi.
...                 Tests may change the primary slot; the teardown restores it.

Resource            ../resources/common.resource

Test Tags           section-3    slot-change
Test Teardown       Restore Slot State And Collect Diagnostics


*** Test Cases ***
TC-3.1 Interface Get-Current
    ${current}=    Bootloader Interface Get Current
    ${booted}=    Get Booted Bootname
    Should Be Equal    ${current}    ${booted}

TC-3.2 Interface Get-Primary
    ${primary}=    Bootloader Interface Get Primary
    ${bl}=    Get Bootloader State
    Should Be Equal    ${primary}    ${bl}[active]

TC-3.3 Interface Get-State
    ${bl}=    Get Bootloader State
    FOR    ${bootname}    IN    A    B
        ${state}=    Bootloader Interface Get State    ${bootname}
        ${expected}=    Set Variable If    '${bl}[slots][${bootname}]' == 'normal'    good    bad
        Should Be Equal    ${state}    ${expected}    State of slot ${bootname} (bootloader: ${bl}[slots][${bootname}])
    END

TC-3.4 Interface Set-Primary Round Trip
    [Documentation]    Primary slot can be switched and restored.
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    Bootloader Interface Set Primary    ${y}
    ${primary}=    Bootloader Interface Get Primary
    Should Be Equal    ${primary}    ${y}
    ${bl}=    Get Bootloader State
    Should Be Equal    ${bl}[active]    ${y}
    Bootloader Interface Set Primary    ${x}
    ${primary}=    Bootloader Interface Get Primary
    Should Be Equal    ${primary}    ${x}
    ${bl}=    Get Bootloader State
    Should Be Equal    ${bl}[active]    ${x}

TC-3.5 Interface Set-State Good On Running Slot
    ${x}=    Get Booted Bootname
    Bootloader Interface Set State    ${x}    good
    ${state}=    Bootloader Interface Get State    ${x}
    Should Be Equal    ${state}    good

TC-3.6 Interface Set-State Good On Non-Running Slot Is A No-Op
    [Documentation]    Devices whose bootloader interface ignores 'good' for the non-running slot (Jetson backend)
    ...    must leave its state untouched; not applicable for devices that restore it (U-Boot).
    Require Feature    mark-good-inactive-noop
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    ${before}=    Get Bootloader State
    Bootloader Interface Set State    ${y}    good
    ${after}=    Get Bootloader State
    Should Be Equal    ${after}[slots][${y}]    ${before}[slots][${y}]    Status of the non-running slot changed
    Should Be Equal    ${after}[active]    ${before}[active]

TC-3.7 Interface Set-State Bad Redirects Boot To The Other Slot
    [Documentation]    Marking the active slot bad switches the active slot to the other one.
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    Bootloader Interface Set Primary    ${x}
    ${primary}=    Bootloader Interface Get Primary
    Should Be Equal    ${primary}    ${x}
    Bootloader Interface Set State    ${x}    bad
    ${primary}=    Bootloader Interface Get Primary
    Should Be Equal    ${primary}    ${y}    Active slot was not redirected
    Bootloader Interface Set Primary    ${x}
    ${primary}=    Bootloader Interface Get Primary
    Should Be Equal    ${primary}    ${x}

TC-3.8 Interface Invalid Input Handling
    ${before}=    Get Bootloader State
    ${invalid}=    Get Bootloader Interface Invalid Requests
    FOR    ${cmd}    IN    @{invalid}
        ${r}=    Run On Target    ${cmd}    expected_rc=any
        Should Not Be Equal As Integers    ${r.rc}    0    '${cmd}' must fail
    END
    ${usage}=    Get Bootloader Interface Usage Request
    IF    $usage
        ${r}=    Run On Target    ${usage}[0]    expected_rc=any
        Should Not Be Equal As Integers    ${r.rc}    0    Calling '${usage}[0]' without arguments must fail
        Should Contain    ${r.stderr}    ${usage}[1]
    END
    ${after}=    Get Bootloader State
    Should Be Equal    ${after}    ${before}    Bootloader state changed on invalid input
