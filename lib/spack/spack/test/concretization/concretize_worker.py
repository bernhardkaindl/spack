# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from pathlib import Path
import socket
import sys

import pytest

import spack.sandbox
from spack.solver.concretize_worker import (
    SandboxedConcretizationError,
    _load_response,
    concretize_one_sandboxed,
)


def _prepend_recipe_code(repo_builder, package, code):
    recipe = Path(repo_builder._recipe_filename(package))
    recipe.write_text(f"{code}\n{recipe.read_text(encoding='utf-8')}", encoding="utf-8")


def test_response_rejects_duplicate_keys():
    with pytest.raises(SandboxedConcretizationError, match="duplicate JSON key"):
        _load_response(b'{"protocol_version":1,"ok":true,"ok":false}')


def test_concretize_one_sandboxed_round_trip(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-dependency")
    repo_builder.add_package(
        "sandbox-root", dependencies=[("sandbox-dependency", None, None)]
    )

    concrete = concretize_one_sandboxed(
        "sandbox-root@1.0",
        repositories=[repo_builder.root, mock_packages_repo],
    )

    assert concrete.concrete
    assert concrete.name == "sandbox-root"
    assert concrete["sandbox-dependency"].concrete
    assert concrete.namespace == repo_builder.namespace


def test_recipe_import_cannot_write_outside_private_state(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    sentinel = tmp_path / "outside-sandbox"
    repo_builder.add_package("sandbox-write")
    _prepend_recipe_code(
        repo_builder,
        "sandbox-write",
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('modified')",
    )

    with pytest.raises(SandboxedConcretizationError, match="Permission denied"):
        concretize_one_sandboxed(
            "sandbox-write@1.0", repositories=[repo_builder.root, mock_packages_repo]
        )

    assert not sentinel.exists()


def test_recipe_import_can_write_inside_private_state(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-private-write")
    _prepend_recipe_code(
        repo_builder,
        "sandbox-private-write",
        "from pathlib import Path\n(Path.home() / 'recipe-output').write_text('allowed')",
    )

    concrete = concretize_one_sandboxed(
        "sandbox-private-write@1.0", repositories=[repo_builder.root, mock_packages_repo]
    )

    assert concrete.concrete


def test_recipe_import_cannot_connect_tcp(
    concretize_scope, mock_packages_repo, repo_builder
):
    try:
        sandbox = spack.sandbox.get_sandbox()
    except spack.sandbox.SandboxError as error:
        pytest.skip(str(error))
    if sandbox.abi_version < 4:
        pytest.skip(f"TCP restrictions require Landlock ABI 4+, found ABI {sandbox.abi_version}")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        repo_builder.add_package("sandbox-network")
        _prepend_recipe_code(
            repo_builder,
            "sandbox-network",
            f"import socket\nsocket.create_connection(('127.0.0.1', {port}))",
        )

        with pytest.raises(SandboxedConcretizationError, match="Permission denied"):
            concretize_one_sandboxed(
                "sandbox-network@1.0", repositories=[repo_builder.root, mock_packages_repo]
            )


def test_recipe_import_timeout(concretize_scope, mock_packages_repo, repo_builder):
    repo_builder.add_package("sandbox-timeout")
    _prepend_recipe_code(repo_builder, "sandbox-timeout", "while True:\n    pass")

    with pytest.raises(SandboxedConcretizationError, match="timed out"):
        concretize_one_sandboxed(
            "sandbox-timeout@1.0",
            repositories=[repo_builder.root, mock_packages_repo],
            timeout=0.25,
        )


def test_hash_reference_is_rejected(mock_packages_repo):
    with pytest.raises(SandboxedConcretizationError, match="hash references"):
        concretize_one_sandboxed("/abc123", repositories=[mock_packages_repo])


def test_recipe_diagnostic_output_is_bounded(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-output")
    _prepend_recipe_code(repo_builder, "sandbox-output", "print('x' * (3 * 1024 * 1024))")

    with pytest.raises(SandboxedConcretizationError, match="diagnostic output"):
        concretize_one_sandboxed(
            "sandbox-output@1.0", repositories=[repo_builder.root, mock_packages_repo]
        )


def test_recipe_descendants_are_killed(
    concretize_scope, mock_packages_repo, repo_builder
):
    with socket.socket(type=socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(1.25)
        port = listener.getsockname()[1]
        child_code = (
            "import socket,time; time.sleep(0.75); "
            f"socket.socket(type=socket.SOCK_DGRAM).sendto(b'alive', ('127.0.0.1', {port}))"
        )
        repo_builder.add_package("sandbox-descendant")
        _prepend_recipe_code(
            repo_builder,
            "sandbox-descendant",
            "import subprocess,sys\n"
            f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])",
        )

        concrete = concretize_one_sandboxed(
            "sandbox-descendant@1.0", repositories=[repo_builder.root, mock_packages_repo]
        )
        assert concrete.concrete
        with pytest.raises(socket.timeout):
            listener.recv(16)