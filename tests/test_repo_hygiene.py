
"""Things that are true of the repo rather than of the code.

Small, but this is the second "documented and broken" bug of its kind: the README
says to run `./scripts/mutation_test.py` and the file was not executable, so the
command in the docs failed with Permission denied for anyone who copied it.

The Windows dev box cannot notice - `os.access(path, os.X_OK)` is meaningless
there and the working tree carries no mode bits - so the check has to ask git,
which stores the bit either way.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _tracked_modes() -> dict[str, str]:
    """Path -> git mode, e.g. "100755". Empty if this isn't a git checkout."""
    git = shutil.which("git")
    if git is None:
        return {}
    try:
        out = subprocess.run(
            [git, "ls-files", "-s"], cwd=REPO_ROOT, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}

    modes = {}
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        # "100755 <sha> 0\tpath"
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if path and parts:
            modes[path] = parts[0]
    return modes


def test_every_script_with_a_shebang_is_executable():
    """A shebang is a promise that `./the/script` works. Keep it."""
    modes = _tracked_modes()
    if not modes:
        pytest.skip("not a git checkout, or git unavailable")

    offenders = []
    for path, mode in sorted(modes.items()):
        full = REPO_ROOT / path
        if not full.is_file():
            continue
        try:
            first = full.open("rb").readline()
        except OSError:
            continue
        if first.startswith(b"#!") and mode != "100755":
            offenders.append(f"{path} (mode {mode})")

    assert offenders == [], (
        "these declare a shebang but are not executable, so `./<path>` fails:\n  "
        + "\n  ".join(offenders)
        + "\nFix with: git update-index --chmod=+x <path>"
    )


def test_the_check_can_actually_see_the_repo():
    """Guards the test above: an empty mode map would make it vacuously pass, and
    it is skipped rather than failed when git is missing - so assert that on a
    normal checkout it really did find files."""
    modes = _tracked_modes()
    if not modes:
        pytest.skip("not a git checkout, or git unavailable")
    assert len(modes) > 20
    assert any(path.endswith(".py") for path in modes)


def test_no_working_tree_file_has_crlf_endings():
    """CRLF in the working tree is invisible to everything except the tools that
    matter, so the suite has to be the thing that sees it.

    `.gitattributes` pins `* text=auto eol=lf`, which protects the *repository*:
    Git writes LF on checkout and normalises on commit. It does nothing about a
    file rewritten in place afterwards - and on Windows that is easy to do by
    accident, because `Path.write_text` and `open(..., "w")` translate `\\n` to
    `\\r\\n` unless you pass `newline=""`.

    It happened: a scripted edit rewrote all of `agent/methods.py` that way. The
    commit would still have been LF, so nothing downstream broke - but
    `mutation_test.py` reads files as bytes on purpose, so every multi-line
    anchor reported STALE, which reads as "your guard moved" rather than "your
    file has CRLF". And *this* file could not see it either: the test below
    reads with `read_text()`, which applies universal newlines and makes CRLF
    invisible. Green suite, broken harness.

    The scan lives in `scripts/check_line_endings.py` so the fix is one command
    rather than a manual sweep, and so both callers ask the same question.
    Deliberately not auto-fixed from here: a test run that rewrites source files
    is the same class of surprise as the ad-hoc mutation script that once
    stranded a sabotaged guard on disk. Detecting is the suite's job; changing
    files is something you ask for.
    """
    import importlib.util

    script = REPO_ROOT / "scripts" / "check_line_endings.py"
    spec = importlib.util.spec_from_file_location("check_line_endings", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if shutil.which("git") is None:
        pytest.skip("git unavailable")

    bad = module.offenders(REPO_ROOT)
    assert bad == [], (
        "these have non-LF line endings in the working tree:\n  "
        + "\n  ".join(f"{state:5} {path}" for path, state in bad)
        + "\nFix with: python scripts/check_line_endings.py --fix"
    )

