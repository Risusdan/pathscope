# Data Path Explorer

Visual data-path debug tool. Core engine + CLI (M0-M2); UI follows.

## Quick start (dev)

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    .venv/bin/pytest              # no hardware needed
    .venv/bin/python -m cli probe # needs Blackpill + ST-Link

## M1 poll-rate measurement

Setup: Blackpill (STM32F411CE), clone ST-Link v2, macOS.
Command: `python -m cli monitor --seconds 15`
Result: <PASTE THE M1 RESULT LINE HERE>

## Probe notes

<record any driver/permission fixes discovered during bring-up>
