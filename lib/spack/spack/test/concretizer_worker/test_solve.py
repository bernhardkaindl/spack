# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os
import socket
import subprocess
import warnings

import pytest

import spack.binary_distribution as binary_distribution
import spack.compilers.config as compiler_config
import spack.compilers.libraries as compiler_libraries
import spack.concretize as spack_concretize
import spack.concretizer_worker as concretizer_worker
import spack.config as spack_config
import spack.error as spack_error
import spack.platforms as spack_platforms
import spack.repo as spack_repo
import spack.util.sandbox
from spack.old_installer import PackageInstaller
from spack.spec import Spec
from spack.util.sandbox import JsonWorkerError


@pytest.mark.parametrize(
    "spec_str",
    [
        "pkg-a",
        "pkg-a@1.0",
        "mpi",
        "mpileaks ^zmpi",
        "multivalue-variant libs=static",
        "externaltool",
        'pkg-a cflags="-O -foo-flag foo-val" platform=test %gcc',
    ],
)
def test_one_shot_worker_matches_direct_solve(mock_packages, spec_str):
    direct = spack_concretize.concretize_one(spec_str)

    response = concretizer_worker.solve_in_worker([Spec(spec_str)])

    assert len(response.specs) == 1
    assert response.specs[0].dag_hash() == direct.dag_hash()


def test_worker_preserves_test_dependency_selection(mock_packages):
    abstract = Spec("test-dependency")
    direct = spack_concretize.concretize_one(abstract, tests=True)

    response = concretizer_worker.solve_in_worker([abstract], tests=True)

    assert response.specs[0].dag_hash() == direct.dag_hash()


def test_worker_matches_direct_multi_root_together_solve(mock_packages):
    from spack.solver.asp import Solver

    abstract = [Spec("pkg-a"), Spec("pkg-b")]
    direct = Solver().solve(abstract).specs or []

    worker = concretizer_worker.solve_in_worker(abstract).specs

    assert [spec.dag_hash() for spec in worker] == [spec.dag_hash() for spec in direct]


def test_worker_uses_configured_response_limit(mock_packages, mutable_config, monkeypatch):
    configured_limit = 123456789
    mutable_config.set("config:sandbox:concretizer:max_response_bytes", configured_limit)
    real_runner = spack.util.sandbox.run_json_worker_streaming
    observed = []

    def run_worker(request, worker, setup=None, timeout=None, max_response_bytes=None):
        observed.append((timeout, max_response_bytes))
        return real_runner(
            request, worker, setup=setup, timeout=timeout, max_response_bytes=max_response_bytes
        )

    monkeypatch.setattr(spack.util.sandbox, "run_json_worker_streaming", run_worker)

    concretizer_worker.solve_in_worker([Spec("pkg-a")])

    assert observed == [(None, configured_limit)]


def test_compiler_preflight_includes_configured_and_installed(mock_packages, monkeypatch):
    import spack.concretizer_worker.solve as worker_solve

    configured = spack_concretize.concretize_one("gcc")
    installed = spack_concretize.concretize_one("llvm")
    not_a_compiler = spack_concretize.concretize_one("pkg-a")
    warmed = []

    class Detector:
        def __init__(self, compiler):
            self.compiler = compiler

        def compiler_verbose_output(self):
            warmed.append(self.compiler.dag_hash())

    monkeypatch.setattr(compiler_config, "supported_compilers", lambda: ["gcc", "llvm"])
    monkeypatch.setattr(compiler_libraries, "CompilerPropertyDetector", Detector)
    monkeypatch.setattr(spack_platforms, "using_libc_compatibility", lambda: True)

    worker_solve._preflight_compiler_properties(
        [configured], [configured, installed, not_a_compiler]
    )

    assert warmed == [configured.dag_hash(), installed.dag_hash()]


def test_worker_inherits_existing_solver_timeout_config(
    mock_packages, mutable_config, monkeypatch
):
    import warnings

    import spack.solver.asp

    mutable_config.set("concretizer:timeout", 17)
    mutable_config.set("concretizer:error_on_timeout", False)
    real_solve = spack.solver.asp.Solver.solve

    def report_timeout_config(self, specs, **kwargs):
        warnings.warn(
            "timeout={0},error={1}".format(
                spack_config.CONFIG.get("concretizer:timeout"),
                spack_config.CONFIG.get("concretizer:error_on_timeout"),
            )
        )
        return real_solve(self, specs, **kwargs)

    monkeypatch.setattr(spack.solver.asp.Solver, "solve", report_timeout_config)

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        response = concretizer_worker.solve_in_worker([Spec("pkg-a")])

    assert response.warnings == ["timeout=17,error=False"]


def test_solve_request_returns_warnings_in_order(mock_packages, monkeypatch):
    import warnings

    import spack.solver.asp

    real_solve = spack.solver.asp.Solver.solve

    def solve_with_warnings(self, specs, **kwargs):
        import warnings

        warnings.warn("first warning")
        warnings.warn("second warning")
        return real_solve(self, specs, **kwargs)

    monkeypatch.setattr(spack.solver.asp.Solver, "solve", solve_with_warnings)
    request = concretizer_worker.create_request([Spec("pkg-a")])

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        response = concretizer_worker.solve_request(request)
    restored = concretizer_worker.validate_response(response, request)

    assert restored.warnings == ["first warning", "second warning"]


def test_worker_uses_existing_concretization_cache(use_concretization_cache):
    abstract = Spec("pkg-a")

    first = concretizer_worker.solve_in_worker([abstract])
    entries_after_first = list(use_concretization_cache.iterdir())
    second = concretizer_worker.solve_in_worker([abstract])

    assert entries_after_first
    assert second.specs[0].dag_hash() == first.specs[0].dag_hash()


def test_worker_uses_frozen_buildcache_snapshot(mock_packages, monkeypatch):
    import spack.solver.reuse

    parent_pid = os.getpid()
    concrete = spack_concretize.concretize_one("pkg-a")
    calls = []

    def refresh():
        calls.append(os.getpid())
        return [concrete]

    monkeypatch.setattr(binary_distribution, "update_cache_and_get_specs", refresh)
    monkeypatch.setattr(spack.solver.reuse, "buildcache_reuse_enabled", lambda configuration: True)

    response = concretizer_worker.solve_in_worker([Spec("pkg-a")])

    assert response.specs[0].concrete
    assert calls == [parent_pid]


def test_worker_confines_selected_recipe_import(mock_packages, monkeypatch, tmp_path):
    parent_pid = os.getpid()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("host data")
    original_get_pkg_class = spack_repo.PATH.get_pkg_class
    parent_imports = []

    def get_pkg_class_after_probes(*args, **kwargs):
        if os.getpid() == parent_pid:
            parent_imports.append(kwargs.get("pkg_name", args[0] if args else None))
            return original_get_pkg_class(*args, **kwargs)

        errnos = []
        for operation in (
            lambda: unrelated.read_text(),
            lambda: unrelated.write_text("modified"),
            lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM),
            lambda: subprocess.run(["/bin/true"], check=True),
        ):
            try:
                result = operation()
            except OSError as error:
                errnos.append(error.errno)
            else:
                if isinstance(result, socket.socket):
                    result.close()
                errnos.append(None)
        warnings.warn("recipe-probe={0}".format(",".join(str(value) for value in errnos)))
        return original_get_pkg_class(*args, **kwargs)

    monkeypatch.setattr(spack_repo.PATH, "get_pkg_class", get_pkg_class_after_probes)

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        response = concretizer_worker.solve_in_worker([Spec("pkg-a")])

    probe = next(warning for warning in response.warnings if warning.startswith("recipe-probe="))
    assert all(int(value) in (1, 13) for value in probe.partition("=")[2].split(","))
    assert "pkg-a" not in parent_imports
    assert unrelated.read_text() == "host data"


def test_worker_matches_direct_automatic_splice(mutable_config, install_mockery):
    mutable_config.set("concretizer:reuse", True)
    old = spack_concretize.concretize_one(
        "splice-t@1 ^splice-h@1.0.0+compat ^splice-z@1.0.0+compat"
    )
    PackageInstaller([old.package], fake=True, explicit=True).install()
    mutable_config.set("packages", {"splice-t": {"buildable": False}})
    mutable_config.set("concretizer:splice", {"automatic": True})
    goal = Spec("splice-t@1 ^splice-h@1.0.1+compat ^splice-z@1.0.0+compat")

    direct = spack_concretize.concretize_one(goal)
    worker = concretizer_worker.solve_in_worker([goal]).specs[0]

    assert direct.build_spec is not direct
    assert worker.build_spec is not worker
    assert worker.dag_hash() == direct.dag_hash()
    assert worker.build_spec.dag_hash() == direct.build_spec.dag_hash()


def test_solve_request_supports_separate_strategy(mock_packages):
    request = concretizer_worker.create_request(
        [Spec("pkg-a")], strategy=concretizer_worker.SEPARATELY
    )

    response = concretizer_worker.validate_response(
        concretizer_worker.solve_request(request), request
    )

    assert len(response.specs) == 1
    assert response.specs[0].concrete


def test_worker_preserves_unknown_input_error(mock_packages):
    with pytest.raises(spack_error.UnsatisfiableSpecError) as direct_error:
        spack_concretize.concretize_one("does-not-exist")

    with pytest.raises(spack_error.UnsatisfiableSpecError) as worker_error:
        concretizer_worker.solve_in_worker([Spec("does-not-exist")])

    assert str(worker_error.value) == str(direct_error.value)


def test_worker_preserves_unsatisfiable_category(mock_packages):
    with pytest.raises(spack_error.UnsatisfiableSpecError, match="failed to concretize"):
        concretizer_worker.solve_in_worker([Spec("unsat-provider@1.0+foo")])


def test_worker_preserves_invalid_variant_category(mock_packages):
    with pytest.raises(spack_error.SpecError) as direct_error:
        spack_concretize.concretize_one("pkg-a+does_not_exist")

    with pytest.raises(spack_error.SpecError) as worker_error:
        concretizer_worker.solve_in_worker([Spec("pkg-a+does_not_exist")])

    assert str(worker_error.value) == str(direct_error.value)


def test_worker_reports_unexpected_internal_error(mock_packages, monkeypatch):
    import spack.solver.asp

    def fail_solve(self, specs, **kwargs):
        raise RuntimeError("internal failure")

    monkeypatch.setattr(spack.solver.asp.Solver, "solve", fail_solve)

    with pytest.raises(JsonWorkerError, match="internal failure"):
        concretizer_worker.solve_in_worker([Spec("pkg-a")])


def test_worker_preserves_config_error_category(mock_packages, monkeypatch):
    import spack.solver.asp

    def fail_solve(self, specs, **kwargs):
        raise spack_error.ConfigError("invalid solver configuration")

    monkeypatch.setattr(spack.solver.asp.Solver, "solve", fail_solve)

    with pytest.raises(spack_error.ConfigError, match="invalid solver configuration"):
        concretizer_worker.solve_in_worker([Spec("pkg-a")])


def test_worker_preserves_package_error_category(mock_packages, monkeypatch):
    import spack.solver.asp

    def fail_solve(self, specs, **kwargs):
        raise spack_error.PackageError("invalid package metadata")

    monkeypatch.setattr(spack.solver.asp.Solver, "solve", fail_solve)

    with pytest.raises(spack_error.PackageError, match="invalid package metadata"):
        concretizer_worker.solve_in_worker([Spec("pkg-a")])


def test_worker_preserves_assertion_error_category(mock_packages, monkeypatch):
    import spack.solver.asp

    def fail_solve(self, specs, **kwargs):
        raise AssertionError("invalid solver invariant")

    monkeypatch.setattr(spack.solver.asp.Solver, "solve", fail_solve)

    with pytest.raises(AssertionError, match="invalid solver invariant"):
        concretizer_worker.solve_in_worker([Spec("pkg-a")])
