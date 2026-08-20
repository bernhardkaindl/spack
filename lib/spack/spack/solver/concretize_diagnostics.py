# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Bounded diagnostic fingerprints for comparing concretizer process state.

The fingerprint identifies the complete generated ASP program without returning it. Callers can
also request bounded canonical excerpts grouped by the leading predicate on each line. Excerpts can
contain complete facts or fragments of multiline rules and are intended only for targeted debugging.
"""

import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Union

import spack.config
import spack.platforms
import spack.solver.asp
from spack.solver.compat import clingo, clingo_flavor, default_clingo_options
from spack.spec import ArchSpec, Spec


DIAGNOSTIC_SCHEMA_VERSION = 1
_PREDICATE = re.compile(r"^\s*([a-z][A-Za-z0-9_]*)\s*\(")
MAX_DIAGNOSTIC_EXCERPTS = 5000


def _digest(data: Union[str, bytes]) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _canonical_lines(lines: Iterable[str]):
    return sorted(line for line in lines if line)


def _program_fingerprint(program: str, excerpt_predicates: Iterable[str]) -> Dict[str, Any]:
    lines = _canonical_lines(program.splitlines())
    predicates: Dict[str, list] = {}
    for line in lines:
        match = _PREDICATE.match(line)
        predicates.setdefault(match.group(1) if match else "<other>", []).append(line)
    requested = set(excerpt_predicates)
    excerpts = {
        name: values for name, values in sorted(predicates.items()) if name in requested
    }
    if sum(len(values) for values in excerpts.values()) > MAX_DIAGNOSTIC_EXCERPTS:
        raise RuntimeError("requested solver diagnostic excerpts exceed the limit")
    return {
        "sha256": _digest("\n".join(lines)),
        "line_count": len(lines),
        "predicates": {
            name: {"sha256": _digest("\n".join(values)), "line_count": len(values)}
            for name, values in sorted(predicates.items())
        },
        "excerpts": excerpts,
    }


def _logic_fingerprint() -> Dict[str, str]:
    directory = Path(spack.solver.asp.__file__).parent
    return {path.name: _digest(path.read_bytes()) for path in sorted(directory.glob("*.lp"))}


def _configuration_fingerprint() -> Dict[str, str]:
    result = {}
    for section in ("concretizer", "packages", "compilers"):
        value = spack.config.CONFIG.deepcopy_as_builtin(section)
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        result[section] = _digest(encoded)
    return result


def concretization_fingerprint(
    spec: Union[str, Spec],
    *,
    tests: Union[bool, list] = False,
    excerpt_predicates: Iterable[str] = (),
) -> Dict[str, Any]:
    """Fingerprint the complete generated ASP input and solver runtime without solving."""
    requested = spec if isinstance(spec, Spec) else Spec(spec)
    output = io.StringIO()
    solver = spack.solver.asp.Solver()
    solver.solve_with_stats([requested], out=output, tests=tests, setup_only=True)
    clingo_module = clingo()
    if clingo_module.__file__ is None:
        raise RuntimeError("Clingo module has no filesystem identity")
    module_path = Path(clingo_module.__file__).resolve()
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "platform": {
            "name": spack.platforms.host().name,
            "default_arch": str(ArchSpec.default_arch()),
        },
        "clingo": {
            "flavor": clingo_flavor().name.lower(),
            "version": str(getattr(clingo_module, "__version__", "unknown")),
            "module_sha256": _digest(module_path.read_bytes()),
            "options": default_clingo_options(),
        },
        "configuration": _configuration_fingerprint(),
        "solver_logic": _logic_fingerprint(),
        "asp": _program_fingerprint(output.getvalue(), excerpt_predicates),
    }


def concretization_fingerprint_differences(
    left: Any, right: Any, path: str = ""
) -> Dict[str, Any]:
    """Return differing fingerprint values keyed by their dotted diagnostic path."""
    if isinstance(left, dict) and isinstance(right, dict):
        result = {}
        for key in left.keys() | right.keys():
            result.update(
                concretization_fingerprint_differences(
                    left.get(key), right.get(key), f"{path}.{key}" if path else key
                )
            )
        return result
    return {} if left == right else {path: (left, right)}