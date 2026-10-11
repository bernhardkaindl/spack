#!/usr/bin/env python3
"""Rewrite every commit in BASE..TOP through a per-commit tree transform.

Usage: rewrite_series.py TRANSFORM.py BASE TOP OUTMAP

TRANSFORM.py defines WATCH (list of repository paths) and transform(files, commit), where files
maps each watched path that exists in the commit to (mode, bytes). transform returns the new
mapping for those paths: omitted paths are deleted, new paths are added. Each commit is
transformed independently from its own tree, so use it for mechanical edits that apply to every
layer (imports, markers, renames), not for fixes that belong to one layer.

Raw commit objects are copied with only the tree and parent lines replaced (gpgsig dropped), so
GitButler change-id headers, authors and messages are preserved. OUTMAP receives one
"OLDSHA NEWSHA" line per commit, oldest first; feed it to apply_map.py and validate-layers.sh.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile


def git(*a, inp=None, env=None):
    return subprocess.run(["git", *a], input=inp, capture_output=True, check=True, env=env).stdout


tpath, base, top, outmap = sys.argv[1:]
spec = importlib.util.spec_from_file_location("t", tpath)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
revs = git("rev-list", "--reverse", "--topo-order", f"{base}..{top}").decode().split()
newof = {}
with tempfile.TemporaryDirectory() as td:
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(td, "index"))
    for c in revs:
        git("read-tree", c, env=env)
        files = {}
        for p in t.WATCH:
            r = subprocess.run(
                ["git", "ls-tree", c, "--", p], capture_output=True, text=True, check=True
            ).stdout
            if r:
                mode, _, sha = r.split("\t")[0].split()
                files[p] = (mode, git("cat-file", "blob", sha))
        new = t.transform(dict(files), c)
        info = []
        for p in files:
            if p not in new:
                info.append(f"0 {'0' * 40}\t{p}\0")
        for p, (mode, data) in new.items():
            if files.get(p) != (mode, data):
                sha = git("hash-object", "-w", "--stdin", inp=data).decode().strip()
                info.append(f"{mode} {sha}\t{p}\0")
        if info:
            git("update-index", "-z", "--index-info", inp="".join(info).encode(), env=env)
        tree = git("write-tree", env=env).decode().strip()
        hdr, msg = git("cat-file", "commit", c).decode().split("\n\n", 1)
        lines, skip = [], False
        for line in hdr.split("\n"):
            if skip and line.startswith(" "):
                continue
            skip = False
            if line.startswith("gpgsig"):
                skip = True
                continue
            if line.startswith("tree "):
                line = "tree " + tree
            elif line.startswith("parent "):
                parent = line.split()[1]
                line = "parent " + newof.get(parent, parent)
            lines.append(line)
        newof[c] = (
            git(
                "hash-object",
                "-t",
                "commit",
                "-w",
                "--stdin",
                inp=("\n".join(lines) + "\n\n" + msg).encode(),
            )
            .decode()
            .strip()
        )
with open(outmap, "w") as f:
    for c in revs:
        f.write(f"{c} {newof[c]}\n")
print(f"rewrote {len(revs)} commits; top -> {newof[revs[-1]]}")
