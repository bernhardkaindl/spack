# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import sys

import spack.concretize
from spack.install_worker.learning import (
    denied_executable,
    enabled,
    learn_executable,
    learn_network_destination,
    matching_executables,
    matching_network_destinations,
)
from spack.util.executable import which_string


def test_learning_disabled_by_default():
    assert not enabled({})


def test_matching_executables_resolves_tools_for_matching_package(mock_packages):
    spec = spack.concretize.concretize_one("trivial-install-test-package")
    config = {
        "whitelists": {
            "tools": {"allow": ["true"], "specs": ["trivial-install-test-package"]},
            "other": {"allow": ["false"], "specs": ["libelf"]},
        }
    }

    assert matching_executables(spec, config) == [os.path.realpath(which_string("true"))]


def test_denied_executable_requires_traced_candidate_and_log_match(tmp_path):
    log = tmp_path / "build.log"
    log.write_text("sh: /usr/bin/true: Permission denied\n")

    assert denied_executable(str(log), ["/usr/bin/true"]) == "/usr/bin/true"
    assert denied_executable(str(log), ["/usr/bin/false"]) is None


def test_learn_executable_updates_selected_loaded_scope(mock_packages, mutable_config):
    config_file = mutable_config.get_config_filename("spack", "config")
    mutable_config.set("config:sandbox:learning:enabled", True)
    mutable_config.set("config:sandbox:learning:config_file", config_file)
    spec = spack.concretize.concretize_one("trivial-install-test-package")

    canonical = learn_executable(spec, sys.executable)

    whitelist = mutable_config.get(
        "config:sandbox:whitelists:learned-trivial-install-test-package", scope="spack"
    )
    assert whitelist == {"allow": [canonical], "specs": [spec.name]}


def test_network_groups_match_and_persist_by_host(mock_packages, mutable_config):
    config_file = mutable_config.get_config_filename("spack", "config")
    mutable_config.set("config:sandbox:learning:enabled", True)
    mutable_config.set("config:sandbox:learning:config_file", config_file)
    spec = spack.concretize.concretize_one("trivial-install-test-package")

    canonical = learn_network_destination(spec, "HTTPS://GitHub.COM/path")

    whitelist = mutable_config.get(
        "config:sandbox:whitelists:network-allow-github-com", scope="spack"
    )
    assert canonical == "https://github.com:443"
    assert whitelist == {"network": [canonical], "specs": [spec.name]}
    assert matching_network_destinations(spec, mutable_config.get("config:sandbox")) == [canonical]
