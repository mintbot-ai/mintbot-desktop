#!/usr/bin/env python3
"""The promote gate as a dispatched release answers it.

scripts/release.sh runs ``python3 $AXP_PROMOTE_CHECK --package P --version
DEV --commit SHA`` before it stamps or signs anything on the package's own
channel. The real gate is mintbot's developer_tools/axp_promote_check.py; it
needs the mintbot checkout and the DEV Beaver run store, which a GitHub Actions
runner has neither of. When .github/workflows/release.yml runs release.sh, the
LIVE CatOffice that dispatched it has already run the real gate and passes its
evidence line as the workflow input ``gate_evidence`` (environment
``AXP_GATE_EVIDENCE`` here). This stand-in accepts only that: an evidence line
that names this very package, dev build and commit, and says the journey
passed. Without one it refuses, so a by-hand release.sh still needs the real
gate (set MINTBOT_REPO or AXP_PROMOTE_CHECK to it).

Canonical text: developer_tools/axp_release_workflow/promote_gate_dispatched.py
in the mintbot repository.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

DEV_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+-dev\.\d{14}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="promote gate stand-in for a dispatched release")
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    evidence = os.environ.get("AXP_GATE_EVIDENCE", "").strip()
    if not evidence:
        print("promote gate: REFUSED: no AXP_GATE_EVIDENCE; this stand-in only answers a release dispatched "
              "by CatOffice. By hand, run mintbot's developer_tools/axp_promote_check.py (MINTBOT_REPO).",
              file=sys.stderr)
        return 1
    if not DEV_VERSION_RE.match(args.version):
        print(f"promote gate: REFUSED: {args.version} is not a dev build version", file=sys.stderr)
        return 1
    expected = f"{args.package} {args.version} (commit {args.commit[:12].lower()})"
    if not evidence.lower().startswith(expected.lower()) or " passed" not in evidence:
        print(f"promote gate: REFUSED: the dispatched evidence is not for {expected}: {evidence[:200]}",
              file=sys.stderr)
        return 1
    print(f"promote gate: OK (dispatched) {evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
