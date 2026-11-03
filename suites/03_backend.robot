*** Settings ***
Documentation       Section 3 - Bootloader backend (rauc-nvbootctrl-backend).
...                 Each command is executed directly to isolate the backend from RAUC.
...                 Tests may change the active bootloader slot; the teardown restores it.

Resource            ../resources/common.resource

Test Tags           section-3    slot-change
Test Teardown       Restore Slot State And Collect Diagnostics


*** Test Cases ***
TC-3.1 Backend Get-Current
    ${r}=    Run On Target    ${BACKEND} get-current
    ${booted}=    Get Booted Bootname
    Should Be Equal    ${r.stdout}    ${booted}

TC-3.2 Backend Get-Primary
    ${r}=    Run On Target    ${BACKEND} get-primary
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${r.stdout}    ${nv}[active]

TC-3.3 Backend Get-State
    ${nv}=    Get Nvbootctrl Info
    FOR    ${bootname}    IN    A    B
        ${r}=    Run On Target    ${BACKEND} get-state ${bootname}
        ${expected}=    Set Variable If    '${nv}[slots][${bootname}]' == 'normal'    good    bad
        Should Be Equal    ${r.stdout}    ${expected}    State of slot ${bootname} (nvbootctrl: ${nv}[slots][${bootname}])
    END

TC-3.4 Backend Set-Primary Round Trip
    [Documentation]    Primary slot can be switched and restored.
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    Run On Target    ${BACKEND} set-primary ${y}
    ${r}=    Run On Target    ${BACKEND} get-primary
    Should Be Equal    ${r.stdout}    ${y}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${y}
    Run On Target    ${BACKEND} set-primary ${x}
    ${r}=    Run On Target    ${BACKEND} get-primary
    Should Be Equal    ${r.stdout}    ${x}
    ${nv}=    Get Nvbootctrl Info
    Should Be Equal    ${nv}[active]    ${x}

TC-3.5 Backend Set-State Good On Running Slot
    ${x}=    Get Booted Bootname
    Run On Target    ${BACKEND} set-state ${x} good
    ${r}=    Run On Target    ${BACKEND} get-state ${x}
    Should Be Equal    ${r.stdout}    good

TC-3.6 Backend Set-State Good On Non-Running Slot Is A No-Op
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    ${before}=    Get Nvbootctrl Info
    Run On Target    ${BACKEND} set-state ${y} good
    ${after}=    Get Nvbootctrl Info
    Should Be Equal    ${after}[slots][${y}]    ${before}[slots][${y}]    Status of the non-running slot changed
    Should Be Equal    ${after}[active]    ${before}[active]

TC-3.7 Backend Set-State Bad Redirects Boot To The Other Slot
    [Documentation]    Marking the active slot bad switches the active slot to the other one.
    ${x}=    Get Booted Bootname
    ${y}=    Other Bootname    ${x}
    Run On Target    ${BACKEND} set-primary ${x}
    ${r}=    Run On Target    ${BACKEND} get-primary
    Should Be Equal    ${r.stdout}    ${x}
    Run On Target    ${BACKEND} set-state ${x} bad
    ${r}=    Run On Target    ${BACKEND} get-primary
    Should Be Equal    ${r.stdout}    ${y}    Active slot was not redirected
    Run On Target    ${BACKEND} set-primary ${x}
    ${r}=    Run On Target    ${BACKEND} get-primary
    Should Be Equal    ${r.stdout}    ${x}

TC-3.8 Backend Invalid Input Handling
    ${before}=    Get Nvbootctrl Info
    @{invalid}=    Create List
    ...    ${BACKEND} get-state C
    ...    ${BACKEND} set-primary Z
    ...    ${BACKEND} set-state A maybe
    FOR    ${cmd}    IN    @{invalid}
        ${r}=    Run On Target    ${cmd}    expected_rc=any
        Should Not Be Equal As Integers    ${r.rc}    0    '${cmd}' must fail
    END
    ${r}=    Run On Target    ${BACKEND}    expected_rc=any
    Should Not Be Equal As Integers    ${r.rc}    0    Calling the backend without arguments must fail
    Should Contain    ${r.stderr}    Usage
    ${after}=    Get Nvbootctrl Info
    Should Be Equal    ${after}    ${before}    nvbootctrl state changed on invalid input
