# AGENTS.md

## Project purpose

Roadrunner is an RP2040-Zero filament-motion sensor with a Klippy extra.  The
firmware supports I2C, UART, and USB serial transport; the Klippy extra turns
its encoder and filament-presence data into runout and under-extrusion signals.

## Before changing code

- Preserve user changes.  Check `git status --short` before editing and do not
  overwrite an existing local change without asking.
- Read the firmware, the matching Klippy transport code, and the relevant
  documentation together.  A protocol or register-map change must update both
  sides and the README.
- Treat connected printer hardware as production hardware.  Do not flash a
  toolhead or installed sensor as a test target; use a bench RP2040-Zero with a
  known BOOTSEL recovery path.

## RP2040 identity and flashing

- RP2040 BOOTSEL/flash-derived USB serials are not guaranteed unique on these
  boards.  Never use one as a persistent board identity and never select a
  write by a colliding serial.
- USB topology may be used only as a transient, confirmed handoff while a
  known device re-enumerates.  Do not persist it as a board record.
- A future provisioned UUID must live in a flash sector reserved from both the
  application image and the bootloader.  Normal firmware updates must preserve
  it.
- Katapult compatibility is an end-to-end change: install it through BOOTSEL,
  link the application at Katapult's application offset, and provide a tested
  application command to request the bootloader.  Do not copy the current
  origin-zero UF2 over a Katapult installation.

## Build and verification

- Initialise the SDK before building: `git submodule update --init --recursive`.
- Build from `rp2040/` in an ignored build directory, for example:
  `cmake -S . -B build && cmake --build build`.
- Run `git diff --check` before handing off a change.
- There is no automated firmware test suite yet.  State this plainly and
  describe the exact bench-hardware validation that remains.

## Documentation and commits

- Keep the root README's `## Features` checkbox list and `## TODO` section
  immediately after `## Contents`.
- Document protocol, wiring, configuration, and flashing changes in the root
  README or the relevant `rp2040/` document in the same change.
- Use a conventional commit prefix and a concise lowercase subject when a
  commit is requested.

## Branching and releases

- `main` is the protected release branch.  Do not push to it directly; merge a
  reviewed pull request from `develop` instead.
- `develop` is the integration branch for everyday work.  Use short-lived
  `feat/...`, `fix/...`, or `chore/...` branches and open pull requests into
  `develop`.
- Keep force-pushes and branch deletion disabled for both protected branches.
- Required automated checks will be added only after the corresponding tests
  and GitHub Actions workflows exist and are reliable.
