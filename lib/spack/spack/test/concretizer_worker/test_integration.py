# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import warnings

import pytest

import spack.concretize
import spack.concretizer_worker as concretizer_worker
import spack.config
import spack.environment as ev
import spack.installer_dispatch
import spack.util.parallel
import spack.util.sandbox
from spack.main import SpackCommand
from spack.spec import Spec


def test_shared_together_path_uses_worker(mock_packages, monkeypatch):
    calls = []
    real_solve = concretizer_worker.solve_in_worker

    def solve_in_worker(specs, **kwargs):
        calls.append((specs, kwargs))
        return real_solve(specs, **kwargs)

    monkeypatch.setattr(concretizer_worker, "solve_in_worker", solve_in_worker)

    result = spack.concretize.concretize_together([(Spec("pkg-a"), None)])

    assert calls
    assert result[0][1].concrete


def test_concretize_one_uses_worker_for_virtual_root(mock_packages, monkeypatch):
    calls = []
    real_solve = concretizer_worker.solve_in_worker

    def solve_in_worker(specs, **kwargs):
        calls.append(specs)
        return real_solve(specs, **kwargs)

    monkeypatch.setattr(concretizer_worker, "solve_in_worker", solve_in_worker)

    result = spack.concretize.concretize_one("mpi")

    assert calls
    assert result.concrete


def test_shared_together_path_uses_configured_fallback(mock_packages, monkeypatch):
    monkeypatch.setattr(
        concretizer_worker,
        "select_execution",
        lambda: concretizer_worker.ConcretizerWorkerSelection(
            concretizer_worker.FALLBACK, "unsupported"
        ),
    )
    monkeypatch.setattr(
        concretizer_worker,
        "solve_in_worker",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker selected")),
    )

    result = spack.concretize.concretize_together([(Spec("pkg-a"), None)])

    assert result[0][1].concrete


def test_shared_together_path_replays_worker_warnings(mock_packages, monkeypatch):
    direct = spack.concretize.concretize_one("pkg-a")
    monkeypatch.setattr(
        concretizer_worker,
        "solve_in_worker",
        lambda *args, **kwargs: concretizer_worker.ConcretizerWorkerResponse(
            [direct], ["worker warning"], [0.0]
        ),
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        spack.concretize.concretize_together([(Spec("pkg-a"), None)])

    assert [str(item.message) for item in caught] == ["worker warning"]


def test_when_possible_uses_one_worker_and_reports_results(mock_packages, monkeypatch):
    calls = []
    real_solve = concretizer_worker.solve_in_worker

    def solve_in_worker(specs, **kwargs):
        calls.append(kwargs["strategy"])
        return real_solve(specs, **kwargs)

    monkeypatch.setattr(concretizer_worker, "solve_in_worker", solve_in_worker)
    specs = [(Spec("libdwarf%gcc"), None), (Spec("libdwarf%clang"), None)]

    result = spack.concretize.concretize_together_when_possible(specs)

    assert calls == [concretizer_worker.WHEN_POSSIBLE]
    assert len(result) == 2
    assert all(concrete.concrete for _, concrete in result)


def test_separate_uses_bounded_worker_scheduler(mock_packages, mutable_config, monkeypatch):
    observed = []
    real_map = spack.util.sandbox.map_json_workers_streaming

    def map_workers(requests, worker, **kwargs):
        observed.append((len(requests), kwargs["processes"]))
        return real_map(requests, worker, **kwargs)

    monkeypatch.setattr(spack.util.parallel, "ENABLE_PARALLELISM", True)
    monkeypatch.setattr(spack.config, "determine_number_of_jobs", lambda parallel: 2)
    monkeypatch.setattr(spack.util.sandbox, "map_json_workers_streaming", map_workers)

    result = spack.concretize.concretize_separately([(Spec("pkg-a"), None), (Spec("pkg-b"), None)])

    assert observed == [(2, 2)]
    assert len(result) == 2
    assert all(concrete.concrete for _, concrete in result)


def test_separate_worker_preserves_direct_concrete_input(mock_packages):
    concrete = spack.concretize.concretize_one("pkg-a")

    result = spack.concretize.concretize_separately([(concrete, None), (Spec("pkg-b"), None)])

    assert [abstract.name for abstract, _ in result] == ["pkg-a", "pkg-b"]
    assert result[0][1] == concrete
    assert result[1][1].concrete


def test_separate_uses_configured_fallback(mock_packages, monkeypatch):
    monkeypatch.setattr(
        concretizer_worker,
        "select_execution",
        lambda: concretizer_worker.ConcretizerWorkerSelection(
            concretizer_worker.FALLBACK, "unsupported"
        ),
    )
    monkeypatch.setattr(
        concretizer_worker,
        "solve_separately_in_workers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker selected")),
    )

    result = spack.concretize.concretize_separately([(Spec("pkg-a"), None)])

    assert result[0][1].concrete


@pytest.mark.disable_clean_stage_check
def test_install_implicitly_concretizes_in_worker(mock_packages, monkeypatch):
    worker_calls = []
    installed_specs = []
    real_solve = concretizer_worker.solve_in_worker

    def solve_in_worker(specs, **kwargs):
        worker_calls.append([str(spec) for spec in specs])
        return real_solve(specs, **kwargs)

    class RecordingInstaller:
        reports = {}

        def install(self):
            pass

    def create_installer(packages, **kwargs):
        installed_specs.extend(package.spec for package in packages)
        return RecordingInstaller()

    monkeypatch.setattr(concretizer_worker, "solve_in_worker", solve_in_worker)
    monkeypatch.setattr(spack.installer_dispatch, "create_installer", create_installer)

    SpackCommand("install")("pkg-a")

    assert worker_calls == [["pkg-a"]]
    assert installed_specs and all(spec.concrete for spec in installed_specs)


def test_concretize_command_does_not_write_lockfile_after_invalid_worker_response(
    tmp_path, mock_packages, monkeypatch
):
    environment = ev.create_in_dir(tmp_path)
    environment.add("pkg-a")
    environment.write()
    real_runner = spack.util.sandbox.run_json_worker_streaming

    def invalid_response(*args, **kwargs):
        response = real_runner(*args, **kwargs)
        response["results"][0]["duration"] = -1
        return response

    monkeypatch.setattr(spack.util.sandbox, "run_json_worker_streaming", invalid_response)

    with environment:
        with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError):
            SpackCommand("concretize")()

    assert not environment.concretized_roots
    assert not environment.specs_by_hash
    assert not (tmp_path / ev.lockfile_name).exists()


def test_already_concretized_environment_does_not_launch_worker(
    tmp_path, mock_packages, monkeypatch
):
    environment = ev.create_in_dir(tmp_path)
    environment.add("pkg-a")
    environment.concretize()
    monkeypatch.setattr(
        concretizer_worker,
        "solve_in_worker",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker launched")),
    )

    assert environment.concretize() == []
