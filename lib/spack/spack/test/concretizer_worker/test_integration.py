# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import warnings

import spack.concretize
import spack.concretizer_worker as concretizer_worker
import spack.config
import spack.util.parallel
import spack.util.sandbox
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
