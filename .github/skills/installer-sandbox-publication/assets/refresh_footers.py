#!/usr/bin/env python3
"""Refresh the GitButler stack footer on the installer-sandbox series PRs.

PR numbers come only from the ``number`` field of published-prs.json, never from
layer positions. Every listed PR must still match its inventory entry on GitHub
(number, head branch, and ``installer-sandbox <position>/18:`` title) before any
write happens. If one check fails, nothing is edited. Dry run by default.

Usage:
  refresh_footers.py [--inventory PATH] [--apply --backup-dir DIR]
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = "spack/spack"
TOTAL = 18
TOP = "<!-- GitButler Footer Boundary Top -->"
BOT = "<!-- GitButler Footer Boundary Bottom -->"
BLOCK = re.compile(re.escape(TOP) + r".*?" + re.escape(BOT) + r"\n?", re.S)
DEFAULT_INVENTORY = Path(__file__).with_name("published-prs.json")


def gh(*args, stdin=None):
    return subprocess.run(
        ["gh", *args], input=stdin, text=True, capture_output=True, check=True
    ).stdout


def load_inventory(path):
    entries = json.loads(Path(path).read_text())
    positions = sorted(e["position"] for e in entries)
    if positions != list(range(1, len(entries) + 1)):
        sys.exit(f"inventory positions must be 1..N without gaps: {positions}")
    if len({e["number"] for e in entries}) != len(entries):
        sys.exit("inventory has duplicate PR numbers")
    return sorted(entries, key=lambda e: e["position"])


def footer(entries, current):
    lines = [TOP, "---", f"This is PR {current['position']}/{TOTAL} in a stack:"]
    for entry in reversed(entries):
        marker = " 👈" if entry is current else ""
        lines.append(f"- <kbd>&nbsp;{entry['position']}&nbsp;</kbd> #{entry['number']}{marker}")
    lines.append(BOT)
    return "\n".join(lines) + "\n"


def with_footer(body, entries, current):
    prose = BLOCK.sub("", body).rstrip()
    return prose + "\n\n" + footer(entries, current)


def live_pr(number):
    return json.loads(
        gh("pr", "view", str(number), "--repo", REPO, "--json", "number,title,headRefName,body")
    )


def verify(entries):
    """Return (entry, live_body) pairs, or exit before any write."""
    checked = []
    problems = []
    for entry in entries:
        live = live_pr(entry["number"])
        prefix = f"installer-sandbox {entry['position']}/{TOTAL}:"
        if live["number"] != entry["number"]:
            problems.append(f"#{entry['number']}: number mismatch")
        if live["headRefName"] != entry["branch"]:
            problems.append(
                f"#{entry['number']}: head is {live['headRefName']}, "
                f"inventory says {entry['branch']}"
            )
        if not live["title"].startswith(prefix):
            problems.append(f"#{entry['number']}: title does not start with '{prefix}'")
        checked.append((entry, live["body"]))
    if problems:
        sys.exit("refusing to edit, inventory does not match GitHub:\n  " + "\n  ".join(problems))
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--apply", action="store_true", help="write the footers")
    parser.add_argument(
        "--backup-dir", type=Path, help="required with --apply: original bodies are saved here"
    )
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")

    entries = load_inventory(args.inventory)
    plan = []
    for entry, old_body in verify(entries):
        new_body = with_footer(old_body, entries, entry)
        changed = new_body.rstrip() != old_body.rstrip()
        plan.append((entry, old_body, new_body, changed))
        status = "update" if changed else "unchanged"
        print(f"#{entry['number']} {entry['position']}/{TOTAL} {entry['branch']}: {status}")

    if not args.apply:
        print("dry run: nothing written (use --apply --backup-dir DIR)")
        return

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    for entry, old_body, new_body, changed in plan:
        if not changed:
            continue
        (args.backup_dir / f"pr-{entry['number']}.md").write_text(old_body)
        gh("pr", "edit", str(entry["number"]), "--repo", REPO, "--body-file", "-", stdin=new_body)
        readback = live_pr(entry["number"])["body"]
        if readback.rstrip() != new_body.rstrip():
            sys.exit(f"read-back mismatch on #{entry['number']}; backup in {args.backup_dir}")
        print(f"#{entry['number']}: written and verified")


if __name__ == "__main__":
    main()
