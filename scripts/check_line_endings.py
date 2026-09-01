#!/usr/bin/env python3
"""Check that no working-tree file has CRLF endings, and optionally fix them.

    ./scripts/check_line_endings.py          # report, exit 1 if any
    ./scripts/check_line_endings.py --fix    # rewrite them as LF

WHY THIS EXISTS
---------------
``.gitattributes`` pins ``* text=auto eol=lf``, so Git writes LF on checkout and
normalises on commit. That protects the *repository*. It does nothing about a
file rewritten in place after checkout - and on Windows that is easy to do by
accident, because ``pathlib.Path.write_text`` and ``open(..., "w")`` translate
``\\n`` to ``\\r\\n`` unless you pass ``newline=""``.

It happened: a scripted edit rewrote all of ``agent/methods.py`` that way. The
committed content would still have been LF, so nothing downstream broke - but
every byte-level tool in this repo stopped matching. ``mutation_test.py`` reads
files as bytes on purpose (a decode step in the wrong place is what once
stranded a sabotaged guard on disk), so every multi-line anchor reported STALE,
which reads as "your guard moved" rather than "your file has CRLF".

The suite could not see it either: ``test_repo_hygiene`` read with
``read_text()``, which applies universal newlines and makes CRLF invisible. That
gap - green suite, broken harness - is what this closes.

WHY IT ASKS GIT
---------------
``git ls-files --eol`` already answers exactly this, and answers it better than
a hand-rolled scan would: it applies the ``.gitattributes`` rules, and it knows
which files are binary (``w/-text``) so a firmware ``.bin`` full of ``\\r\\n``
pairs is never a false positive. Re-implementing either of those here would be a
second copy of a rule Git already owns.

Untracked files are included. A brand new file is exactly where this mistake
lands and stays hidden until its first commit normalises it.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

#: Working-tree states that mean "not LF". `none` is an empty file and `-text`
#: is binary; neither is a problem, and treating them as one would make this
#: something people learn to ignore.
BAD = ("crlf", "mixed")


def _ls_files(root: pathlib.Path, *args: str) -> list[str]:
    """`git ls-files --eol`, NUL-separated so a path with spaces survives."""
    res = subprocess.run(
        ["git", "ls-files", "-z", "--eol", *args],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        return []
    text = res.stdout.decode("utf-8", errors="replace")
    return [record for record in text.split("\0") if record.strip()]


def offenders(root: pathlib.Path) -> list[tuple[str, str]]:
    """(path, working-tree eol) for every file whose working copy is not LF.

    Tracked and untracked both, deduplicated by path - `--others` is a separate
    invocation because Git will not report cached and untracked entries with one
    set of flags.
    """
    found: dict[str, str] = {}
    for args in ((), ("--others", "--exclude-standard")):
        for record in _ls_files(root, *args):
            # "i/lf    w/crlf  attr/text=auto eol=lf \tsrc/foo.py"
            head, _, path = record.rpartition("\t")
            if not path:
                continue
            state = next(
                (f[2:] for f in head.split() if f.startswith("w/")), "lf"
            )
            if state in BAD:
                found[path] = state
    return sorted(found.items())


def fix(root: pathlib.Path, paths: list[str]) -> list[str]:
    """Rewrite each file with LF endings. Returns what it could not fix.

    Bytes in, bytes out, and no decode step anywhere - the same rule
    `mutation_test.py` follows, for the same reason. A file this cannot fix is
    reported rather than left looking done: a lone ``\\r`` is an old-Mac ending
    and turning it into a newline is a guess about somebody's data, not a
    normalisation.
    """
    failed: list[str] = []
    for path in paths:
        target = root / path
        try:
            data = target.read_bytes()
        except OSError as exc:
            failed.append(f"{path}: {exc}")
            continue
        fixed = data.replace(b"\r\n", b"\n")
        if b"\r" in fixed:
            failed.append(f"{path}: has lone CR bytes - not touching it")
            continue
        try:
            target.write_bytes(fixed)
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--fix", action="store_true", help="rewrite offending files as LF"
    )
    args = parser.parse_args(argv)

    root = pathlib.Path(__file__).resolve().parents[1]
    bad = offenders(root)
    if not bad:
        print("every working-tree file is LF.")
        return 0

    for path, state in bad:
        print(f"{state:5} | {path}")

    if not args.fix:
        print(f"\n{len(bad)} file(s) with non-LF endings. Re-run with --fix.", file=sys.stderr)
        return 1

    failed = fix(root, [path for path, _ in bad])
    print(f"\nfixed {len(bad) - len(failed)} file(s).")
    if failed:
        print("could not fix:", file=sys.stderr)
        for line in failed:
            print(f"  - {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
