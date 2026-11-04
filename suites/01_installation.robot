*** Settings ***
Documentation       Section 1 - Installation and static configuration.

Resource            ../resources/common.resource

Test Tags           section-1


*** Test Cases ***
TC-1.1 RAUC Packages Installed
    [Documentation]    The image contains the RAUC client/service, the keyring and the device specific
    ...    bootloader integration (device plugin).
    [Tags]    readonly
    ${r}=    Run On Target    command -v rauc
    Should Not Be Empty    ${r.stdout}
    ${r}=    Run On Target    rauc --version
    Should Match Regexp    ${r.stdout}    \\d+\\.\\d+
    Run On Target    ls -l /etc/rauc/system.conf /etc/rauc/ca.cert.pem
    Device Installation Should Be Complete

TC-1.2 System Conf Contents
    [Documentation]    The build-time placeholders of system.conf were substituted and the slot layout and
    ...    bootloader settings match the design of the device.
    [Tags]    readonly
    ${r}=    Run On Target    cat /etc/rauc/system.conf
    Save Artifact    system.conf.target    ${r.stdout}
    ${conf}=    Parse Ini    ${r.stdout}
    ${placeholders}=    Get System Conf Placeholders
    FOR    ${placeholder}    IN    @{placeholders}
        Should Not Contain    ${r.stdout}    ${placeholder}    Placeholder ${placeholder} was not substituted
    END
    ${expected}=    Get Expected System Conf
    FOR    ${section}    ${items}    IN    &{expected}
        Dictionary Should Contain Key    ${conf}    ${section}    Section [${section}] is missing in system.conf
        FOR    ${key}    ${value}    IN    &{items}
            Dictionary Should Contain Key    ${conf}[${section}]    ${key}    [${section}] ${key} is missing in system.conf
            Should Be Equal    ${conf}[${section}][${key}]    ${value}    system.conf [${section}] ${key}
        END
    END
    # The file must equal the reference version (supplied as input) after the build-time substitution.
    IF    $INPUTS['system_conf']
        ${reference}=    Get File    ${INPUTS}[system_conf]
        FOR    ${placeholder}    ${value}    IN    &{placeholders}
            ${reference}=    Replace String    ${reference}    ${placeholder}    ${value}
        END
        ${a}=    Normalise Text    ${reference}
        ${b}=    Normalise Text    ${r.stdout}
        Should Be Equal    ${b}    ${a}    system.conf on the target differs from the reference expected/system.conf
    ELSE
        Log    expected/system.conf not supplied, skipped the comparison with the reference file    WARN
    END

TC-1.3 Redundant Partition Layout
    [Documentation]    The device was flashed with the A/B layout: both rootfs slot devices exist, have the same
    ...    size, exactly one is the root filesystem and the device specific layout checks pass.
    [Tags]    readonly
    ${dev_a}=    Get Slot Device    A
    ${dev_b}=    Get Slot Device    B
    Run On Target    test -b ${dev_a}
    Run On Target    test -b ${dev_b}
    Run On Target    lsblk -o NAME,PARTLABEL,SIZE,FSTYPE,MOUNTPOINT    expected_rc=any
    ${app}=    Get Slot Block Name    A
    ${app_b}=    Get Slot Block Name    B
    ${a}=    Run On Target    cat /sys/class/block/${app}/size
    ${b}=    Run On Target    cat /sys/class/block/${app_b}/size
    Should Be Equal    ${a.stdout}    ${b.stdout}    The slot partitions of A and B differ in size
    # Exactly one of them is the root filesystem and neither is mounted twice.
    ${root}=    Get Root Block Name
    @{rootfs_parts}=    Create List    ${app}    ${app_b}
    List Should Contain Value    ${rootfs_parts}    ${root}    The root filesystem is on neither slot partition
    ${mounts_a}=    Run On Target
    ...    awk -v d="$(cat /sys/class/block/${app}/dev)" '$3==d && $4=="/"' /proc/self/mountinfo | wc -l
    ${mounts_b}=    Run On Target
    ...    awk -v d="$(cat /sys/class/block/${app_b}/dev)" '$3==d && $4=="/"' /proc/self/mountinfo | wc -l
    ${total}=    Evaluate    int($mounts_a.stdout) + int($mounts_b.stdout)
    Should Be Equal As Integers    ${total}    1    Rootfs partitions must be mounted exactly once in total
    Device Partition Layout Should Be Valid

TC-1.4 Keyring And Signing Chain
    [Documentation]    The keyring on the target is the reference CA (expected/ca.cert.pem) and the bundle's
    ...    signature chains to it (checked by `rauc info` on the target against its own keyring).
    [Tags]    readonly
    ${ca}=    Download File From Target    /etc/rauc/ca.cert.pem    ca.cert.pem.target
    IF    $INPUTS['ca_cert']
        ${target_sha}=    File Sha256    ${ca}
        ${ref_sha}=    File Sha256    ${INPUTS}[ca_cert]
        Should Be Equal    ${target_sha}    ${ref_sha}    Target keyring differs from the reference expected/ca.cert.pem
    ELSE
        Log    expected/ca.cert.pem not supplied, skipped the comparison with the reference CA    WARN
    END
    ${has_openssl}=    Target Has Command    openssl
    IF    ${has_openssl}
        Run On Target    openssl x509 -in /etc/rauc/ca.cert.pem -noout -subject -issuer -dates
        # exit code 0 = the CA certificate is not expired
        Run On Target    openssl x509 -in /etc/rauc/ca.cert.pem -noout -checkend 0
    ELSE
        Log    openssl is not installed on the target, skipped the CA validity check    WARN
    END
    Require Bundle
    ${info}=    Get Bundle Info
    Should Not Be Empty    ${info}[compatible]

TC-1.5 Data Partition
    [Documentation]    The persistent data partition exists and is mounted read-write (ext4) on ${DATA_MOUNT_POINT};
    ...    the device specific layout checks (e.g. it fills the disk) pass.
    [Tags]    readonly
    ${data}=    Get Data Device
    Run On Target    test -b ${data}
    Run On Target    lsblk -o NAME,PARTLABEL,SIZE,FSTYPE,MOUNTPOINT    expected_rc=any
    ${mount}=    Run On Target    awk '$2=="${DATA_MOUNT_POINT}" {print $1, $3, $4}' /proc/mounts
    Should Not Be Empty    ${mount.stdout}    ${DATA_MOUNT_POINT} is not mounted
    Should Match Regexp    ${mount.stdout}    ^\\S+ ext4 (rw|rw,\\S+)$    ${DATA_MOUNT_POINT} must be ext4 and writable
    ${src}=    Run On Target    readlink -f "$(awk '$2=="${DATA_MOUNT_POINT}" {print $1}' /proc/mounts)"
    ${expected}=    Run On Target    readlink -f ${data}
    Should Be Equal    ${src.stdout}    ${expected.stdout}    ${DATA_MOUNT_POINT} is not backed by the data partition ${data}
    Device Data Layout Should Be Valid
    Run On Target    touch ${DATA_MOUNT_POINT}/.rauc-tests-write && rm -f ${DATA_MOUNT_POINT}/.rauc-tests-write
