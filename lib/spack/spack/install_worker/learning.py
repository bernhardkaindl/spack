# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Trusted policy handling for opt-in executable learning."""

import os
import re
from typing import Any, Dict, List, Optional

import spack.config
import spack.error
import spack.spec
from spack.util.executable import which_string
from spack.util.proxy import destination_from_url


class LearningError(spack.error.SpackError):
    """Executable learning configuration or evidence is invalid."""


MAX_LOG_BYTES = 1024 * 1024


def enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether unsafe alpha executable learning is enabled."""
    config = config if config is not None else spack.config.CONFIG.get("config:sandbox", {})
    learning = config.get("learning", {})
    return learning.get("enabled", False) is True


def matching_executables(spec: spack.spec.Spec, config: Dict[str, Any]) -> List[str]:
    """Resolve executable grants from whitelists matching ``spec``."""
    result = []
    for whitelist in config.get("whitelists", {}).values():
        try:
            matches = any(spec.satisfies(selector) for selector in whitelist["specs"])
        except (KeyError, TypeError, spack.error.SpackError) as error:
            raise LearningError("sandbox executable whitelist is invalid: {0}".format(error))
        if not matches:
            continue
        for executable in whitelist.get("allow", []):
            path = executable if os.path.isabs(executable) else which_string(executable)
            if path:
                canonical = os.path.realpath(path)
                if os.path.isfile(canonical) and os.access(canonical, os.X_OK):
                    result.append(canonical)
    return list(dict.fromkeys(result))


def canonical_network_destination(url: str) -> str:
    """Return a canonical proxy destination URL with an explicit port."""
    destination = destination_from_url(url)
    return "{0}://{1}:{2}".format(destination.scheme, destination.host, destination.port)


def matching_network_destinations(spec: spack.spec.Spec, config: Dict[str, Any]) -> List[str]:
    """Return canonical proxy destinations from network groups matching ``spec``."""
    result = []
    for whitelist in config.get("whitelists", {}).values():
        try:
            matches = any(spec.satisfies(selector) for selector in whitelist["specs"])
            destinations = whitelist.get("network", [])
        except (KeyError, TypeError, spack.error.SpackError) as error:
            raise LearningError("sandbox network whitelist is invalid: {0}".format(error))
        if not matches:
            continue
        try:
            result.extend(canonical_network_destination(url) for url in destinations)
        except (TypeError, ValueError, spack.error.SpackError) as error:
            raise LearningError("sandbox network whitelist is invalid: {0}".format(error))
    return list(dict.fromkeys(result))


def denied_executable(log_path: str, candidates: List[str]) -> Optional[str]:
    """Return the newest traced executable named by a bounded denial diagnostic."""
    try:
        with open(log_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - MAX_LOG_BYTES))
            log = stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    for candidate in reversed(candidates):
        basename = os.path.basename(candidate)
        if candidate + ": Permission denied" in log:
            return candidate
        if basename + ": Permission denied" in log:
            return candidate
    return None


def has_permission_denied_hint(log_path: str) -> bool:
    """Return whether the bounded log tail contains a non-authoritative denial hint."""
    try:
        with open(log_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - MAX_LOG_BYTES))
            return b": Permission denied" in stream.read(MAX_LOG_BYTES)
    except OSError:
        return False


def _scope_for_file(config_file: str) -> str:
    target = os.path.realpath(spack.config.canonicalize_path(config_file))
    for name, scope in spack.config.CONFIG.scopes.items():
        if not hasattr(scope, "get_section_filename"):
            continue
        try:
            candidate = spack.config.CONFIG.get_config_filename(name, "config")
        except (NotImplementedError, OSError, ValueError):
            continue
        if os.path.realpath(candidate) == target:
            return name
    raise LearningError("learning config_file is not an already loaded Spack config file")


def learn_executable(spec: spack.spec.Spec, executable: str) -> str:
    """Persist one validated executable grant and return its canonical path."""
    config = spack.config.CONFIG.get("config:sandbox", {})
    if not enabled(config):
        raise LearningError("sandbox executable learning is disabled")
    if not os.path.isabs(executable):
        raise LearningError("learned executable path is not absolute")
    canonical = os.path.realpath(executable)
    if not os.path.isfile(canonical) or not os.access(canonical, os.X_OK):
        raise LearningError("learned executable is not executable outside confinement")

    config_file = config.get("learning", {}).get("config_file")
    if not isinstance(config_file, str) or not config_file:
        raise LearningError("sandbox learning config_file is not configured")
    scope = _scope_for_file(config_file)
    name = "learned-{0}".format(spec.name)
    scope_config = spack.config.CONFIG.get_config("config", scope=scope)
    sandbox = scope_config.setdefault("sandbox", {})
    whitelists = sandbox.setdefault("whitelists", {})
    whitelist = whitelists.setdefault(name, {"allow": [], "specs": [spec.name]})
    if canonical not in whitelist["allow"]:
        whitelist["allow"].append(canonical)
    spack.config.CONFIG.update_config("config", scope_config, scope=scope)
    return canonical


def learn_network_destination(spec: spack.spec.Spec, url: str) -> str:
    """Persist one canonical build-network destination in a reusable host group."""
    config = spack.config.CONFIG.get("config:sandbox", {})
    if not enabled(config):
        raise LearningError("sandbox network learning is disabled")
    try:
        destination = destination_from_url(url)
    except (TypeError, ValueError, spack.error.SpackError) as error:
        raise LearningError("learned network destination is invalid: {0}".format(error))
    canonical = "{0}://{1}:{2}".format(destination.scheme, destination.host, destination.port)

    config_file = config.get("learning", {}).get("config_file")
    if not isinstance(config_file, str) or not config_file:
        raise LearningError("sandbox learning config_file is not configured")
    scope = _scope_for_file(config_file)
    name = "network-allow-{0}".format(re.sub(r"[^a-z0-9]+", "-", destination.host).strip("-"))
    scope_config = spack.config.CONFIG.get_config("config", scope=scope)
    sandbox = scope_config.setdefault("sandbox", {})
    whitelists = sandbox.setdefault("whitelists", {})
    whitelist = whitelists.setdefault(name, {"network": [], "specs": []})
    network = whitelist.setdefault("network", [])
    specs = whitelist.setdefault("specs", [])
    if canonical not in network:
        network.append(canonical)
    if spec.name not in specs:
        specs.append(spec.name)
    spack.config.CONFIG.update_config("config", scope_config, scope=scope)
    return canonical
