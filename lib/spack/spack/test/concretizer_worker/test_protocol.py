# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import json

import pytest

import spack.concretize
import spack.concretizer_worker as concretizer_worker
import spack.repo
import spack.util.sandbox
from spack.spec import Spec


def test_request_round_trip_does_not_import_recipes(mock_packages, monkeypatch):
    specs = [Spec("pkg-a@1.0 ^pkg-b")]

    def fail_on_recipe_import(*args, **kwargs):
        raise AssertionError("request handling imported a package recipe")

    monkeypatch.setattr(spack.repo.PATH, "get_pkg_class", fail_on_recipe_import)

    request = concretizer_worker.create_request(
        specs, tests=["pkg-a"], allow_deprecated=True, strategy="together"
    )
    restored = concretizer_worker.validate_request(request)

    json.dumps(request, allow_nan=False)
    assert [str(spec) for spec in restored.specs] == [str(spec) for spec in specs]
    assert restored.tests == ["pkg-a"]
    assert restored.allow_deprecated is True
    assert restored.strategy == "together"


def test_request_preserves_compiler_virtual_constraints():
    spec = Spec("mpileaks ^libdwarf %gcc ^mpich %[virtuals=fortran] gcc %clang")

    restored = concretizer_worker.validate_request(
        concretizer_worker.create_request([spec])
    ).specs[0]

    assert restored == spec
    assert str(restored) == str(spec)


def test_request_rejects_mismatched_spec_representations():
    request = concretizer_worker.create_request([Spec("pkg-a")])
    request["spec_strings"][0] = "pkg-b"

    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError, match="do not match"):
        concretizer_worker.validate_request(request)


@pytest.mark.parametrize("strategy", concretizer_worker.STRATEGIES)
def test_request_accepts_each_strategy(strategy):
    request = concretizer_worker.create_request([Spec("pkg-a")], strategy=strategy)

    assert concretizer_worker.validate_request(request).strategy == strategy


def test_request_round_trips_reuse_snapshots(mock_packages):
    concrete = spack.concretize.concretize_one("pkg-a")
    libc = Spec("glibc@=2.39", external_path="/usr")
    request = concretizer_worker.create_request(
        [Spec("pkg-a")],
        buildcache_specs=[concrete],
        host_libcs=[libc],
        local_store_specs=[concrete],
        local_external_origin_hashes=[concrete.dag_hash()],
        local_deprecated_for={concrete.dag_hash(): "replacement-hash"},
    )

    restored = concretizer_worker.validate_request(request)

    assert restored.buildcache_specs[0].dag_hash() == concrete.dag_hash()
    assert str(restored.host_libcs[0]) == "glibc@=2.39"
    assert restored.host_libcs[0].external_path == "/usr"
    assert restored.local_store_specs[0].dag_hash() == concrete.dag_hash()
    assert restored.local_external_origin_hashes == [concrete.dag_hash()]
    assert restored.local_deprecated_for == {concrete.dag_hash(): "replacement-hash"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"buildcache_specs": [Spec("pkg-a")]},
        {"host_libcs": [Spec("glibc@2.39:", external_path="/usr")]},
        {"host_libcs": [Spec("glibc@=2.39")]},
        {"host_libcs": [Spec("zlib@=1.3", external_path="/usr")]},
        {"local_store_specs": [Spec("pkg-a")]},
        {"local_external_origin_hashes": [""]},
        {"local_deprecated_for": {"hash": ""}},
        {"local_deprecated_for": []},
    ],
)
def test_request_rejects_invalid_reuse_snapshots(kwargs):
    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError):
        concretizer_worker.create_request([Spec("pkg-a")], **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"local_external_origin_hashes": ["unknown-hash"]},
        {"local_deprecated_for": {"unknown-hash": "replacement-hash"}},
    ],
)
def test_request_rejects_unmatched_local_metadata(kwargs):
    request = concretizer_worker.create_request([Spec("pkg-a")])
    request.update(kwargs)

    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError, match="unmatched local"):
        concretizer_worker.validate_request(request)


def test_large_native_request_uses_scalable_transport():
    spec = Spec("pkg-a")
    large_flag = "x" * spack.util.sandbox.MAX_MESSAGE_BYTES
    spec.compiler_flags.add_flag("cflags", large_flag, False, large_flag, "command_line")
    request = concretizer_worker.create_request([spec])

    assert len(json.dumps(request).encode("utf-8")) > spack.util.sandbox.MAX_MESSAGE_BYTES
    transferred = spack.util.sandbox.run_json_worker_streaming(
        request, lambda message: message, timeout=10
    )

    restored = concretizer_worker.validate_request(transferred)
    assert str(restored.specs[0].compiler_flags["cflags"][0]) == large_flag


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {
            "allow_deprecated": False,
            "protocol": concretizer_worker.PROTOCOL_VERSION,
            "specs": [],
            "strategy": "together",
            "tests": False,
            "unexpected": True,
        },
        {
            "allow_deprecated": False,
            "protocol": True,
            "specs": [],
            "strategy": "together",
            "tests": False,
        },
        {
            "allow_deprecated": False,
            "protocol": concretizer_worker.PROTOCOL_VERSION + 1,
            "specs": [],
            "strategy": "together",
            "tests": False,
        },
        {
            "allow_deprecated": False,
            "protocol": concretizer_worker.PROTOCOL_VERSION,
            "specs": [],
            "strategy": "invalid",
            "tests": False,
        },
        {
            "allow_deprecated": 0,
            "protocol": concretizer_worker.PROTOCOL_VERSION,
            "specs": [{}],
            "strategy": "together",
            "tests": False,
        },
        {
            "allow_deprecated": False,
            "protocol": concretizer_worker.PROTOCOL_VERSION,
            "specs": {},
            "strategy": "together",
            "tests": False,
        },
        {
            "allow_deprecated": False,
            "protocol": concretizer_worker.PROTOCOL_VERSION,
            "specs": [[]],
            "strategy": "together",
            "tests": False,
        },
    ],
)
def test_request_rejects_malformed_input(payload):
    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError):
        concretizer_worker.validate_request(payload)


@pytest.mark.parametrize("tests", ["pkg-a", [""], [1], None])
def test_request_rejects_invalid_tests(tests):
    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError):
        concretizer_worker.create_request([Spec("pkg-a")], tests=tests)


def test_response_round_trip_does_not_import_recipes(mock_packages, monkeypatch):
    abstract_specs = [Spec("pkg-a"), Spec("pkg-b")]
    concrete_specs = [spack.concretize.concretize_one(spec) for spec in abstract_specs]
    request = concretizer_worker.create_request(abstract_specs)

    def fail_on_recipe_import(*args, **kwargs):
        raise AssertionError("response handling imported a package recipe")

    monkeypatch.setattr(spack.repo.PATH, "get_pkg_class", fail_on_recipe_import)

    response = concretizer_worker.create_response(concrete_specs, warnings=["warning"])
    restored = concretizer_worker.validate_response(response, request)

    json.dumps(response, allow_nan=False)
    assert [spec.dag_hash() for spec in restored.specs] == [
        spec.dag_hash() for spec in concrete_specs
    ]
    assert restored.warnings == ["warning"]


def test_contract_round_trip_over_scalable_transport(mock_packages):
    abstract = Spec("pkg-a")
    concrete = spack.concretize.concretize_one(abstract)
    request = concretizer_worker.create_request([abstract])
    response = concretizer_worker.create_response([concrete])

    transferred = spack.util.sandbox.run_json_worker_streaming(
        request, lambda received: response, timeout=10
    )
    restored = concretizer_worker.validate_response(transferred, request)

    assert restored.specs[0].dag_hash() == concrete.dag_hash()


def test_response_rejects_non_concrete_spec():
    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError, match="concrete specs"):
        concretizer_worker.create_response([Spec("pkg-a")])


def test_response_rejects_empty_specs():
    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError, match="invalid specs"):
        concretizer_worker.create_response([])


@pytest.mark.parametrize("warnings", ["warning", [1], ["x" * (1024 * 1024)], [""] * 10000])
def test_response_rejects_invalid_or_excessive_warnings(warnings):
    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError):
        concretizer_worker.create_response([], warnings=warnings)


def test_response_rejects_mismatched_hash(mock_packages):
    abstract = Spec("pkg-a")
    concrete = spack.concretize.concretize_one(abstract)
    request = concretizer_worker.create_request([abstract])
    response = concretizer_worker.create_response([concrete])
    response["results"][0]["dag_hash"] = "a" * len(response["results"][0]["dag_hash"])

    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError, match="does not match"):
        concretizer_worker.validate_response(response, request)


def test_response_round_trips_spliced_spec(mock_packages):
    abstract = Spec("splice-t")
    target = spack.concretize.concretize_one(abstract)
    replacement = spack.concretize.concretize_one("splice-h+foo")
    spliced = target.splice(replacement, transitive=True)
    request = concretizer_worker.create_request([abstract])

    restored = concretizer_worker.validate_response(
        concretizer_worker.create_response([spliced]), request
    )

    assert restored.specs[0].dag_hash() == spliced.dag_hash()
    assert restored.specs[0].build_spec.dag_hash() == spliced.build_spec.dag_hash()


def test_response_rejects_reordered_results(mock_packages):
    abstract_specs = [Spec("pkg-a"), Spec("pkg-b")]
    concrete_specs = [spack.concretize.concretize_one(spec) for spec in abstract_specs]
    request = concretizer_worker.create_request(abstract_specs)
    response = concretizer_worker.create_response(concrete_specs)
    response["results"].reverse()

    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError, match="input ordering"):
        concretizer_worker.validate_response(response, request)


def test_response_allows_multiple_inputs_to_share_one_concrete_root(mock_packages):
    abstract = [Spec("pkg-a"), Spec("pkg-a")]
    concrete = spack.concretize.concretize_one("pkg-a")
    request = concretizer_worker.create_request(abstract)

    restored = concretizer_worker.validate_response(
        concretizer_worker.create_response([concrete, concrete]), request
    )

    assert [spec.dag_hash() for spec in restored.specs] == [
        concrete.dag_hash(),
        concrete.dag_hash(),
    ]


def test_error_response_round_trip():
    response = concretizer_worker.create_error_response("unsatisfiable", "cannot solve", "details")

    error = concretizer_worker.validate_error_response(response)

    assert error == concretizer_worker.ConcretizerWorkerErrorResponse(
        "unsatisfiable", "cannot solve", "details"
    )


@pytest.mark.parametrize(
    "response",
    [
        {"error": {}, "protocol": concretizer_worker.PROTOCOL_VERSION},
        {
            "error": {"kind": "forged", "long_message": None, "message": "failure"},
            "protocol": concretizer_worker.PROTOCOL_VERSION,
        },
        {
            "error": {"kind": "spack", "long_message": None, "message": "x" * (2 * 1024 * 1024)},
            "protocol": concretizer_worker.PROTOCOL_VERSION,
        },
    ],
)
def test_error_response_rejects_malformed_or_excessive_data(response):
    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError):
        concretizer_worker.validate_error_response(response)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda response: response.update(unexpected=True),
        lambda response: response.update(protocol=True),
        lambda response: response.update(warnings="warning"),
        lambda response: response.update(results=[]),
        lambda response: response["results"][0].update(input=True),
        lambda response: response["results"][0].pop("duration"),
        lambda response: response["results"][0].update(duration=-1),
        lambda response: response["results"][0].update(duration=True),
        lambda response: response["results"][0].update(duration=float("nan")),
        lambda response: response["results"][0].update(duration=float("inf")),
        lambda response: response["results"][0].update(unexpected=True),
        lambda response: response["results"][0].update(spec=[]),
    ],
)
def test_response_rejects_malformed_input(mock_packages, mutate):
    abstract = Spec("pkg-a")
    concrete = spack.concretize.concretize_one(abstract)
    request = concretizer_worker.create_request([abstract])
    response = concretizer_worker.create_response([concrete])
    mutate(response)

    with pytest.raises(concretizer_worker.ConcretizerWorkerProtocolError):
        concretizer_worker.validate_response(response, request)
