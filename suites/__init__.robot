*** Settings ***
Documentation       RAUC integration test suites. Hardware independent: the device under test is selected with
...                 `--device NAME` / `DEVICE=NAME` and handled by a plugin in libraries/devices/.
...                 Implements RAUC-TEST-PLAN.md. Connection settings come from the env file
...                 or the environment (see rauc-test-suite/README.md and DEVICES.md).

Resource            ../resources/common.resource

Suite Setup         Initialise Test Run
Suite Teardown      Finalise Test Run
Test Teardown       Collect Diagnostics On Failure
