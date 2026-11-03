*** Settings ***
Documentation       RAUC integration test suites for the Jetson Orin Nano devkit (NVMe).
...                 Implements RAUC-TEST-PLAN.md. Connection settings come from the env file
...                 or the environment (see rauc-test-suite/README.md).

Resource            ../resources/common.resource

Suite Setup         Initialise Test Run
Suite Teardown      Finalise Test Run
Test Teardown       Collect Diagnostics On Failure
