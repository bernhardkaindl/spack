# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import warnings

import spack.concretize
import spack.concretizer_worker as concretizer_worker
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
            [direct], ["worker warning"]
        ),
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        spack.concretize.concretize_together([(Spec("pkg-a"), None)])

    assert [str(item.message) for item in caught] == ["worker warning"]
