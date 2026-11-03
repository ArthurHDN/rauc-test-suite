*** Settings ***
Documentation       Section 1 - Installation and static configuration.

Resource            ../resources/common.resource

Test Tags           section-1


*** Test Cases ***
TC-1.1 RAUC Packages Installed
    [Documentation]    The image contains the RAUC client/service and the integration files.
    [Tags]    readonly
    ${r}=    Run On Target    command -v rauc
    Should Not Be Empty    ${r.stdout}
    ${r}=    Run On Target    rauc --version
    Should Match Regexp    ${r.stdout}    \\d+\\.\\d+
    Run On Target    ls -l /etc/rauc/system.conf /etc/rauc/ca.cert.pem ${BACKEND}
    ${r}=    Run On Target    ls -l ${BACKEND}
    Should Match Regexp    ${r.stdout}    ^-rwxr-xr-x
    Run On Target    test -x ${BACKEND}
    Run On Target    command -v nvbootctrl
    Run On Target    test -x /usr/sbin/nvbootctrl

TC-1.2 System Conf Contents
    [Documentation]    The @RAUC_COMPATIBLE@ and @DATA_MOUNT_POINT@ placeholders were substituted and the slot
    ...    layout matches the design.
    [Tags]    readonly
    ${r}=    Run On Target    cat /etc/rauc/system.conf
    Save Artifact    system.conf.target    ${r.stdout}
    ${conf}=    Parse Ini    ${r.stdout}
    Should Not Contain    ${r.stdout}    @RAUC_COMPATIBLE@
    Should Not Contain    ${r.stdout}    @DATA_MOUNT_POINT@
    Should Be Equal    ${conf}[system][compatible]    ${MACHINE}
    Should Be Equal    ${conf}[system][bootloader]    custom
    Should Be Equal    ${conf}[system][bundle-formats]    -plain
    Should Be Equal    ${conf}[system][data-directory]    ${DATA_MOUNT_POINT}/rauc
    Should Be Equal    ${conf}[handlers][bootloader-custom-backend]    ${BACKEND}
    Should Be Equal    ${conf}[slot.rootfs.0][device]    /dev/disk/by-partlabel/APP
    Should Be Equal    ${conf}[slot.rootfs.0][bootname]    A
    Should Be Equal    ${conf}[slot.rootfs.0][type]    ext4
    Should Be Equal    ${conf}[slot.rootfs.1][device]    /dev/disk/by-partlabel/APP_b
    Should Be Equal    ${conf}[slot.rootfs.1][bootname]    B
    Should Be Equal    ${conf}[slot.rootfs.1][type]    ext4
    # The file must equal the reference version (supplied as input) after the build-time substitution.
    IF    $INPUTS['system_conf']
        ${expected}=    Get File    ${INPUTS}[system_conf]
        ${expected}=    Replace String    ${expected}    @RAUC_COMPATIBLE@    ${MACHINE}
        ${expected}=    Replace String    ${expected}    @DATA_MOUNT_POINT@    ${DATA_MOUNT_POINT}
        ${a}=    Normalise Text    ${expected}
        ${b}=    Normalise Text    ${r.stdout}
        Should Be Equal    ${b}    ${a}    system.conf on the target differs from the reference expected/system.conf
    ELSE
        Log    expected/system.conf not supplied, skipped the comparison with the reference file    WARN
    END

TC-1.3 Redundant Partition Layout
    [Documentation]    The device was flashed with the A/B layout, so both rootfs partitions exist.
    [Tags]    readonly
    ${r}=    Run On Target    ls -1 /dev/disk/by-partlabel/
    @{labels}=    Split To Lines    ${r.stdout}
    List Should Contain Value    ${labels}    APP
    List Should Contain Value    ${labels}    APP_b
    Run On Target    lsblk -o NAME,PARTLABEL,SIZE,FSTYPE,MOUNTPOINT    expected_rc=any
    ${a}=    Run On Target    cat /sys/class/block/$(basename "$(readlink -f /dev/disk/by-partlabel/APP)")/size
    ${b}=    Run On Target    cat /sys/class/block/$(basename "$(readlink -f /dev/disk/by-partlabel/APP_b)")/size
    Should Be Equal    ${a.stdout}    ${b.stdout}    APP and APP_b differ in size
    # Exactly one of them is the root filesystem and neither is mounted twice.
    ${root}=    Get Root Block Name
    ${app}=    Get Partlabel Block Name    A
    ${app_b}=    Get Partlabel Block Name    B
    @{rootfs_parts}=    Create List    ${app}    ${app_b}
    List Should Contain Value    ${rootfs_parts}    ${root}    The root filesystem is on neither APP nor APP_b
    ${mounts_a}=    Run On Target
    ...    awk -v d="$(cat /sys/class/block/${app}/dev)" '$3==d && $4=="/"' /proc/self/mountinfo | wc -l
    ${mounts_b}=    Run On Target
    ...    awk -v d="$(cat /sys/class/block/${app_b}/dev)" '$3==d && $4=="/"' /proc/self/mountinfo | wc -l
    ${total}=    Evaluate    int($mounts_a.stdout) + int($mounts_b.stdout)
    Should Be Equal As Integers    ${total}    1    Rootfs partitions must be mounted exactly once in total
    # Bootloader / firmware partitions come in A/B pairs as well; the secondary copy is named
    # B_<name>, <name>_b or <name>_alt (e.g. esp_alt, recovery_alt, recovery-dtb_alt).
    @{paired}=    Create List
    FOR    ${label}    IN    @{labels}
        ${is_b}=    Evaluate    bool(re.search(r'(?i)(^B_|_b$|_alt$)', '${label}')) and '${label}' != 'APP_b'
        IF    ${is_b}    Append To List    ${paired}    ${label}
    END
    Should Not Be Empty    ${paired}    No redundant bootloader/firmware partitions found

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
    [Documentation]    The DATA partition exists, fills the rest of the disk and is mounted read-write on /data.
    [Tags]    readonly
    Run On Target    test -b /dev/disk/by-partlabel/DATA
    Run On Target    lsblk -o NAME,PARTLABEL,SIZE,FSTYPE,MOUNTPOINT    expected_rc=any
    ${mount}=    Run On Target    awk '$2=="/data" {print $1, $3, $4}' /proc/mounts
    Should Not Be Empty    ${mount.stdout}    /data is not mounted
    Should Match Regexp    ${mount.stdout}    ^\\S+ ext4 (rw|rw,\\S+)$    /data must be ext4 and writable
    ${src}=    Run On Target    readlink -f "$(awk '$2=="/data" {print $1}' /proc/mounts)"
    ${data}=    Run On Target    readlink -f /dev/disk/by-partlabel/DATA
    Should Be Equal    ${src.stdout}    ${data.stdout}    /data is not backed by the DATA partition
    # No unallocated space after DATA, only the secondary GPT and alignment may remain (sectors).
    ${free}=    Run On Target
    ...    p=$(basename "$(readlink -f /dev/disk/by-partlabel/DATA)"); d=$(basename "$(readlink -f /sys/class/block/$p/..)"); echo $(( $(cat /sys/class/block/$d/size) - $(cat /sys/class/block/$p/start) - $(cat /sys/class/block/$p/size) ))
    Should Be True    0 <= int(${free.stdout}) <= 4096    ${free.stdout} sectors are left unused after the DATA partition
    # DATA is the largest partition (it takes the space not used by the rootfs slots).
    ${sizes}=    Run On Target
    ...    for f in /sys/class/block/*/partition; do p=$(dirname $f); echo "$(cat $p/size) $(basename $p)"; done | sort -n | tail -n 1
    ${largest}=    Evaluate    $sizes.stdout.split()[1]
    ${data_name}=    Evaluate    $data.stdout.split('/')[-1]
    Should Be Equal    ${largest}    ${data_name}    DATA is not the largest partition
    Run On Target    touch /data/.rauc-tests-write && rm -f /data/.rauc-tests-write
