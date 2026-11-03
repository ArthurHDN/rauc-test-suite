*** Settings ***
Documentation       Section 4 - Bundle inspection.
...                 The bundle is an input (inputs/bundle/*.raucb or --bundle); building it is not part of the
...                 tests. `rauc info` is executed on the target, so the signature is checked against the
...                 target's own keyring.

Resource            ../resources/common.resource

Test Tags           section-4    readonly


*** Test Cases ***
TC-4.1 Bundle Input Is Present And Intact
    [Documentation]    The supplied bundle exists, has a plausible size, and all input files match the
    ...    checksums recorded in inputs.json (when present).
    Require Bundle
    ${size}=    Get File Size    ${BUNDLE}
    Should Be True    ${size} > 10 * 1024 * 1024    Bundle is suspiciously small: ${size} bytes
    ${sha}=    File Sha256    ${BUNDLE}
    Save Artifact    bundle.sha256    ${sha} *${BUNDLE}
    ${problems}=    Verify Inputs    ${INPUTS}
    IF    $INPUTS['metadata_file']
        Should Be Empty    ${problems}    Input files do not match inputs.json: ${problems}
    ELSE
        Log    inputs.json not supplied, input checksums could not be verified    WARN
    END

TC-4.2 Bundle Metadata
    Require Bundle
    ${info}=    Get Bundle Info
    Should Be Equal    ${info}[compatible]    ${MACHINE}
    Should Be Equal As Strings    ${info}[format]    verity
    ${count}=    Get Length    ${info}[images]
    Should Be Equal As Integers    ${count}    1    Expected exactly one image in the bundle
    ${image}=    Set Variable    ${info}[images][0]
    Should Be Equal    ${image}[class]    rootfs
    Should Match Regexp    ${image}[filename]    ^core-image-minimal-.*\\.ext4$
    Should Match Regexp    ${image}[sha256]    ^[0-9a-f]{64}$

TC-4.3 Bundle Uses The Matching Rootfs
    [Documentation]    The bundled rootfs equals the reference checksum supplied in expected/rootfs.sha256.
    Require Bundle
    Require Input    rootfs_sha256    expected/rootfs.sha256
    ${info}=    Get Bundle Info
    Should Be Equal    ${info}[images][0][sha256]    ${INPUTS}[rootfs_sha256]
    ...    Bundle rootfs differs from the reference rootfs checksum (stale bundle?)
