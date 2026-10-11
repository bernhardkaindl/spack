#!/usr/bin/env python3
"""Gate diff-cover JSON results per production file.

Usage: cov_gate.py REPORT.json MIN_FILE_PERCENT MIN_TOTAL_PERCENT NS_REGEX NS_TARGET

Prints one summary line, then one detail line per file below a threshold. Exit status 1 when the
overall result is below MIN_TOTAL_PERCENT or any single file is below MIN_FILE_PERCENT. Files whose path matches NS_REGEX only warn when
below NS_TARGET.
"""

import json
import re
import sys

report, minimum, min_total, ns_regex, ns_target = (
    sys.argv[1],
    float(sys.argv[2]),
    float(sys.argv[3]),
    sys.argv[4],
    float(sys.argv[5]),
)
data = json.load(open(report))
files = data["src_stats"]
total_lines = data["total_num_lines"]
failed = False
details = []

if total_lines == 0:
    print("n/a (no executable lines changed)")
    sys.exit(0)

total = float(data["total_percent_covered"])
if total < min_total:
    failed = True
    details.append(f"overall {total:.2f}% < {min_total:g}%")

for path, stats in sorted(files.items()):
    percent = float(stats["percent_covered"])
    missed = len(stats["violation_lines"])
    name = path.replace("lib/spack/spack/", "")
    if percent < minimum:
        failed = True
        details.append(f"FAIL {name} {percent:.2f}% < {minimum:g}% ({missed} missed)")
    elif re.search(ns_regex, path) and percent < ns_target:
        details.append(f"warn {name} {percent:.2f}% < {ns_target:g}% target ({missed} missed)")

print(f"{total:.2f}% of {total_lines} lines, {len(files)} files")
for line in details:
    print(f"    {line}")
sys.exit(1 if failed else 0)
