#!/usr/bin/env python3
"""Point the series branch refs at the commits of a rewrite map, keeping the GitButler stack.

Usage: apply_map.py MAP TOP_BRANCH REF_PATTERN [REF_PATTERN ...]

MAP holds "OLDSHA NEWSHA" lines (from rewrite_series.py). Every local ref matching a REF_PATTERN
(for example refs/heads/namespace-sandbox/v2/ and refs/heads/namespace-sandbox/v2-review-shaped)
whose current value is an OLDSHA is moved to its NEWSHA, with an old-value check. The stack is
unapplied first and re-applied afterwards, which keeps branch names, stack order and change ids.
Several refs may share one tip (layer 18 and the review-shaped branch), so refs are looked up by
their own current value, never by a lookup keyed on the old SHA alone.
"""

import subprocess
import sys


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def main(argv):
    map_file, top_branch, patterns = argv[0], argv[1], argv[2:]
    with open(map_file, encoding="utf-8") as f:
        moves = dict(line.split() for line in f if line.strip())
    refs = []
    for pattern in patterns:
        for line in run("git", "for-each-ref", "--format=%(refname) %(objectname)", pattern).split(
            "\n"
        ):
            if line:
                refs.append(tuple(line.split()))
    todo = [(ref, old, moves[old]) for ref, old in refs if old in moves]
    if not todo:
        sys.exit("no ref matches an old commit of the map; nothing to do")
    run("but", "unapply", top_branch)
    for ref, old, new in todo:
        run("git", "update-ref", ref, new, old)
        print(f"{ref.replace('refs/heads/', '')} {old[:10]} -> {new[:10]}")
    print(run("but", "apply", top_branch).strip())


if __name__ == "__main__":
    main(sys.argv[1:])
