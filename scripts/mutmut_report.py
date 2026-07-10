#!/usr/bin/env python3
"""Run mutmut across custom_components/src and write a human-readable report.

Mutation testing answers a different question than line coverage: not "did
this line run during the tests" but "would the tests notice if this line's
logic were wrong". A first full run against this codebase found survivors
line coverage doesn't catch — e.g. coordinator.py's `bat.get("cells", [])`
mutated to `bat.get("cells", None)` with nothing failing, because no test
covers a battery dict missing the "cells" key.

Deliberately not a CI gate (no --cov-fail-under equivalent here): mutation
testing also produces "equivalent mutants" that can never be killed no matter
how thorough the tests are (e.g. mutating `cast(dict[str, Any], x)` to
`cast(None, x)` — typing.cast is a runtime no-op either way), so a raw
survival count conflates real test gaps with noise. This script is a
periodic/manual triage aid, not a merge check: read the report, decide which
survivors are real gaps worth a new test versus equivalent mutants worth a
`# pragma: no mutate`, same as reviewing coverage-annotated output.

Usage
-----
    python scripts/mutmut_report.py                    # full run, all of
                                                         # custom_components + src
    python scripts/mutmut_report.py --only-mutate \\
        custom_components/pylontech_mqtt/coordinator.py  # scope to one file

A full run currently takes roughly 30-60 minutes (observed ~1.8-15
mutations/second depending on how much of the HA test harness a module pulls
in); scoping to one file with --only-mutate is much faster for iterating on a
single module.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
REPORT_PATH = ROOT / "mutation-report.md"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)


def _stats() -> dict[str, int]:
    _run(["mutmut", "export-cicd-stats"])
    stats_path = ROOT / "mutants" / "mutmut-cicd-stats.json"
    data: dict[str, int] = json.loads(stats_path.read_text())
    return data


def _survivor_names() -> list[str]:
    # Bare `mutmut results` (no --all) already excludes killed mutants —
    # exactly the "interesting" set (survived/timeout/suspicious) a triage
    # report wants.
    result = _run(["mutmut", "results"])
    return [
        line.strip().rsplit(":", 1)[0]
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def _show(mutant_name: str) -> str:
    return _run(["mutmut", "show", mutant_name]).stdout


def _write_report(stats: dict[str, int], survivors: list[str]) -> None:
    killed = stats["killed"]
    total = stats["total"]
    score = (killed / total * 100) if total else 0.0

    lines = [
        "# Mutation testing report",
        "",
        f"Mutation score: **{score:.1f}%** ({killed}/{total} killed)",
        f"- Survived: {stats['survived']}",
        f"- No tests covered the mutated line: {stats['no_tests']}",
        f"- Timeout: {stats['timeout']}",
        f"- Suspicious: {stats['suspicious']}",
        "",
        "Not every survivor below is a real gap — `typing.cast(...)` and "
        "log-message mutations are commonly equivalent mutants (behavior "
        "identical at runtime). Triage each one: write a test that would "
        "kill it, or mark it `# pragma: no mutate` if it's equivalent.",
        "",
    ]
    for name in survivors:
        lines.append(f"## {name}")
        lines.append("")
        lines.append("```diff")
        lines.append(_show(name).rstrip("\n"))
        lines.append("```")
        lines.append("")

    REPORT_PATH.write_text("\n".join(lines))


@contextmanager
def _with_scoped_config(only_mutate: str | None) -> Generator[None]:
    """Temporarily narrow [tool.mutmut]'s only_mutate in pyproject.toml for one run.

    mutmut has no CLI flag for this (only `run [MUTANT_NAMES]...`, which needs
    mutant IDs that don't exist until after a run) — only_mutate is
    pyproject.toml-only, so scoping a single-file run means editing it and
    restoring the original text afterward.
    """
    if only_mutate is None:
        yield
        return

    original = PYPROJECT.read_text()
    marker = "[tool.mutmut]\n"
    idx = original.index(marker) + len(marker)
    patched = original[:idx] + f'only_mutate = ["{only_mutate}"]\n' + original[idx:]
    PYPROJECT.write_text(patched)
    try:
        yield
    finally:
        PYPROJECT.write_text(original)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only-mutate",
        metavar="PATH",
        help=(
            "Scope this run to a single file, e.g. "
            "custom_components/pylontech_mqtt/coordinator.py"
        ),
    )
    parser.add_argument(
        "--max-children",
        type=int,
        default=None,
        help="Passed through to `mutmut run --max-children`.",
    )
    args = parser.parse_args()

    run_args = ["mutmut", "run"]
    if args.max_children is not None:
        run_args += ["--max-children", str(args.max_children)]

    with _with_scoped_config(args.only_mutate):
        print(f"Running: {' '.join(run_args)}", file=sys.stderr)
        subprocess.run(run_args, cwd=ROOT, check=False)
        stats = _stats()
        survivors = _survivor_names()
        _write_report(stats, survivors)

    total = stats["total"]
    killed = stats["killed"]
    score = (killed / total * 100) if total else 0.0
    print(f"\nMutation score: {score:.1f}% ({killed}/{total} killed)")
    print(f"{len(survivors)} mutant(s) to triage")
    print(f"Full report: {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
