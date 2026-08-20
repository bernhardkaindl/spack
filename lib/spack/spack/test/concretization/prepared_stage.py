# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import base64
import copy
import gzip
import hashlib
import http.server
import io
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path

import pytest

import spack.solver.prepared_stage as prepared_stage_module
from spack.solver.prepared_stage import (
    PreparedStageError,
    SourceFetchPolicy,
    prepare_stage,
    prepared_stage_digest,
    source_plan_digest,
)


@pytest.fixture
def provenance():
    return {
        "dag_hash": "a" * 32,
        "package_hash": "b" * 52 + "====",
        "repositories": [
            {"namespace": "builtin.mock", "package_api": [2, 1], "identity": "c" * 64}
        ],
    }


@pytest.fixture
def fetch_policy(tmp_path):
    return SourceFetchPolicy(file_roots=(tmp_path,))


def source_plan(archive: Path, provenance, *, extension="tar.gz", expand=True):
    return {
        "schema_version": 1,
        "provenance": provenance,
        "source": {
            "kind": "url",
            "urls": [archive.as_uri()],
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "expand": expand,
            "extension": extension,
        },
        "resources": [],
        "patches": [],
    }


def add_resource(
    plan,
    archive: Path,
    *,
    destination="vendor",
    placement="headers",
    extension="tar.gz",
    expand=True,
):
    plan["schema_version"] = 2
    plan["resources"].append(
        {
            "name": "headers",
            "source": {
                "kind": "url",
                "urls": [archive.as_uri()],
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "expand": expand,
                "extension": extension,
            },
            "destination": destination,
            "placement": placement,
        }
    )
    return plan


def add_patch(
    plan,
    content: bytes,
    *,
    owner="test.package",
    level=0,
    working_dir=".",
    reverse=False,
    targets=None,
):
    plan["schema_version"] = 3
    plan["patches"].append(
        {
            "kind": "inline",
            "owner": owner,
            "sha256": hashlib.sha256(content).hexdigest(),
            "level": level,
            "working_dir": working_dir,
            "reverse": reverse,
            "targets": targets or ["file"],
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
    )
    return plan


def add_url_patch(
    plan,
    url: str,
    sha256: str,
    *,
    archive_sha256=None,
    extension=None,
    owner="test.package",
    level=0,
    working_dir=".",
    reverse=False,
):
    plan["schema_version"] = 4
    plan["patches"].append(
        {
            "kind": "url",
            "owner": owner,
            "sha256": sha256,
            "level": level,
            "working_dir": working_dir,
            "reverse": reverse,
            "url": url,
            "archive_sha256": archive_sha256,
            "extension": extension,
        }
    )
    return plan


def write_tar(archive: Path, entries):
    with tarfile.open(archive, "w:gz") as output:
        for name, contents, entry_type in entries:
            info = tarfile.TarInfo(name)
            info.type = entry_type
            if entry_type == tarfile.REGTYPE:
                data = contents.encode("utf-8")
                info.size = len(data)
                info.mode = 0o755
                output.addfile(info, io.BytesIO(data))
            elif entry_type == tarfile.SYMTYPE:
                info.linkname = contents
                output.addfile(info)


def test_prepare_stage_fetches_checks_and_extracts(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    write_tar(archive, [("project/configure", "#!/bin/sh\n", tarfile.REGTYPE)])

    plan = source_plan(archive, provenance)
    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    configure = prepared.path / "configure"
    assert configure.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert configure.stat().st_mode & 0o111
    assert prepared.source_plan_sha256 == source_plan_digest(plan)
    assert prepared.content_sha256 == prepared_stage_digest(prepared.path)
    assert not list(tmp_path.glob(".prepared.preparing-*"))


def test_prepare_stage_fetches_and_places_resource(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(archive, [("project/configure", "#!/bin/sh\n", tarfile.REGTYPE)])
    write_tar(resource, [("include/library.h", "#define VALUE 1\n", tarfile.REGTYPE)])
    plan = add_resource(source_plan(archive, provenance), resource)

    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    header = prepared.path / "vendor" / "headers" / "library.h"
    assert header.read_text(encoding="utf-8") == "#define VALUE 1\n"
    assert prepared.source_plan_sha256 == source_plan_digest(plan)
    assert prepared.content_sha256 == prepared_stage_digest(prepared.path)


def test_prepare_stage_places_resource_mapping(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(archive, [("project/configure", "#!/bin/sh\n", tarfile.REGTYPE)])
    write_tar(
        resource,
        [
            ("resource/include/library.h", "#define VALUE 1\n", tarfile.REGTYPE),
            ("resource/lib/library.txt", "library\n", tarfile.REGTYPE),
        ],
    )
    placement = [
        {"source": "include/library.h", "destination": "headers/library.h"},
        {"source": "lib", "destination": "vendor/lib"},
    ]
    plan = add_resource(source_plan(archive, provenance), resource, placement=placement)
    plan["schema_version"] = 6

    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    assert (prepared.path / "vendor" / "headers" / "library.h").read_text() == (
        "#define VALUE 1\n"
    )
    assert (prepared.path / "vendor" / "vendor" / "lib" / "library.txt").read_text() == (
        "library\n"
    )


def test_prepare_stage_maps_no_expand_query_filename(tmp_path, provenance):
    class ResourceHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"extension\n")

        def log_message(self, format, *args):
            pass

    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "extension-functions.c"
    write_tar(archive, [("project/configure", "#!/bin/sh\n", tarfile.REGTYPE)])
    resource.write_text("extension\n", encoding="utf-8")
    plan = add_resource(
        source_plan(archive, provenance),
        resource,
        placement=[
            {"source": "extension-functions.c?get=25", "destination": "extension-functions.c"}
        ],
        extension=None,
        expand=False,
    )
    plan["schema_version"] = 6
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ResourceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    plan["resources"][0]["source"]["urls"] = [
        f"http://127.0.0.1:{server.server_port}/extension-functions.c?get=25"
    ]
    try:
        prepared = prepare_stage(
            plan,
            tmp_path / "prepared",
            expected_provenance=provenance,
            fetch_policy=SourceFetchPolicy(
                file_roots=(tmp_path,), http_origins=frozenset({("127.0.0.1", server.server_port)})
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert (prepared.path / "vendor" / "extension-functions.c").read_text() == "extension\n"


def test_prepare_stage_prevalidates_resource_mapping_transactionally(
    tmp_path, provenance, fetch_policy, monkeypatch
):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(archive, [("project/vendor/existing", "main\n", tarfile.REGTYPE)])
    write_tar(
        resource,
        [
            ("resource/first", "first\n", tarfile.REGTYPE),
            ("resource/second", "second\n", tarfile.REGTYPE),
        ],
    )
    plan = add_resource(
        source_plan(archive, provenance),
        resource,
        placement=[
            {"source": "first", "destination": "first"},
            {"source": "second", "destination": "existing"},
        ],
    )
    plan["schema_version"] = 6
    copied = []
    original_copy2 = prepared_stage_module.shutil.copy2

    def record_copy(source, destination, *args, **kwargs):
        copied.append((source, destination))
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(prepared_stage_module.shutil, "copy2", record_copy)

    with pytest.raises(PreparedStageError, match="placement already exists"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert copied == []
    assert not (tmp_path / "prepared").exists()


def test_prepare_stage_uses_resource_top_level_directory_for_implicit_placement(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(archive, [("project/configure", "#!/bin/sh\n", tarfile.REGTYPE)])
    write_tar(resource, [("resource-expand/library.h", "#define VALUE 1\n", tarfile.REGTYPE)])
    plan = add_resource(source_plan(archive, provenance), resource, placement=None)
    plan["schema_version"] = 5

    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    header = prepared.path / "vendor" / "resource-expand" / "library.h"
    assert header.read_text(encoding="utf-8") == "#define VALUE 1\n"


def test_prepare_stage_rejects_flat_implicit_resource_transactionally(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(archive, [("project/configure", "#!/bin/sh\n", tarfile.REGTYPE)])
    write_tar(resource, [("library.h", "#define VALUE 1\n", tarfile.REGTYPE)])
    plan = add_resource(source_plan(archive, provenance), resource, placement=None)
    plan["schema_version"] = 5

    with pytest.raises(PreparedStageError, match="one top-level directory"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


def test_prepare_stage_rolls_back_earlier_implicit_resource(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    write_tar(archive, [("project/configure", "#!/bin/sh\n", tarfile.REGTYPE)])
    write_tar(first, [("first/file", "first\n", tarfile.REGTYPE)])
    write_tar(second, [("flat", "second\n", tarfile.REGTYPE)])
    plan = add_resource(source_plan(archive, provenance), first, placement=None)
    second_resource = copy.deepcopy(plan["resources"][0])
    second_resource["name"] = "second"
    second_resource["source"]["urls"] = [second.as_uri()]
    second_resource["source"]["sha256"] = hashlib.sha256(second.read_bytes()).hexdigest()
    plan["resources"].append(second_resource)
    plan["schema_version"] = 5

    with pytest.raises(PreparedStageError, match="one top-level directory"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


@pytest.mark.requires_executables("patch")
def test_prepare_stage_applies_ordered_patches_in_confined_worker(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "source.tar.gz"
    write_tar(archive, [("project/src/file", "before\n", tarfile.REGTYPE)])
    first = b"--- file\n+++ file\n@@ -1 +1 @@\n-before\n+middle\n"
    second = b"--- file\n+++ file\n@@ -1 +1 @@\n-after\n+middle\n"
    plan = source_plan(archive, provenance)
    add_patch(plan, first, working_dir="src")
    add_patch(plan, second, working_dir="src", reverse=True)

    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    assert (prepared.path / "src" / "file").read_text(encoding="utf-8") == "after\n"
    assert prepared.source_plan_sha256 == source_plan_digest(plan)
    assert prepared.content_sha256 == prepared_stage_digest(prepared.path)


@pytest.mark.requires_executables("patch")
def test_prepare_stage_rolls_back_failed_patch_set(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    first = b"--- file\n+++ file\n@@ -1 +1 @@\n-before\n+middle\n"
    failing = b"--- file\n+++ file\n@@ -1 +1 @@\n-missing\n+after\n"
    plan = source_plan(archive, provenance)
    add_patch(plan, first)
    add_patch(plan, failing)

    with pytest.raises(PreparedStageError, match="patch worker failed during apply"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


@pytest.mark.requires_executables("patch")
def test_prepare_stage_fetches_and_applies_url_patch(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    patch_path = tmp_path / "fix.patch"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    content = b"--- file\n+++ file\n@@ -1 +1 @@\n-before\n+after\n"
    patch_path.write_bytes(content)
    plan = source_plan(archive, provenance)
    add_url_patch(plan, patch_path.as_uri(), hashlib.sha256(content).hexdigest())

    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    assert (prepared.path / "file").read_text(encoding="utf-8") == "after\n"
    assert prepared.source_plan_sha256 == source_plan_digest(plan)


@pytest.mark.requires_executables("patch")
def test_prepare_stage_fetches_and_applies_compressed_url_patch(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "source.tar.gz"
    patch_archive = tmp_path / "fix.tar.gz"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    content = "--- file\n+++ file\n@@ -1 +1 @@\n-before\n+after\n"
    write_tar(patch_archive, [("fix.patch", content, tarfile.REGTYPE)])
    plan = source_plan(archive, provenance)
    add_url_patch(
        plan,
        patch_archive.as_uri(),
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        archive_sha256=hashlib.sha256(patch_archive.read_bytes()).hexdigest(),
        extension="tar.gz",
    )

    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    assert (prepared.path / "file").read_text(encoding="utf-8") == "after\n"


@pytest.mark.requires_executables("patch")
def test_prepare_stage_fetches_and_applies_gzip_url_patch(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    patch_archive = tmp_path / "fix.patch.gz"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    content = b"--- file\n+++ file\n@@ -1 +1 @@\n-before\n+after\n"
    with gzip.open(patch_archive, "wb") as output:
        output.write(content)
    plan = source_plan(archive, provenance)
    add_url_patch(
        plan,
        patch_archive.as_uri(),
        hashlib.sha256(content).hexdigest(),
        archive_sha256=hashlib.sha256(patch_archive.read_bytes()).hexdigest(),
        extension="gz",
    )

    prepared = prepare_stage(
        plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
    )

    assert (prepared.path / "file").read_text(encoding="utf-8") == "after\n"


@pytest.mark.requires_executables("gzip")
def test_extract_unix_compress_url_patch(tmp_path):
    archive = Path(__file__).parent.parent / "data" / "compression" / "Foo.Z"
    destination = tmp_path / "payload"
    budget = prepared_stage_module._PreparationBudget(
        max_entries=1, max_expanded_bytes=prepared_stage_module.MAX_PATCH_BYTES
    )

    prepared_stage_module._extract_unix_compress(archive, destination, budget)

    assert destination.read_bytes() == b"TEST\n"


@pytest.mark.requires_executables("gzip")
def test_extract_unix_compress_url_patch_enforces_size_limit(tmp_path):
    archive = Path(__file__).parent.parent / "data" / "compression" / "Foo.Z"
    destination = tmp_path / "payload"
    budget = prepared_stage_module._PreparationBudget(max_entries=1, max_expanded_bytes=3)

    with pytest.raises(PreparedStageError, match="expand beyond"):
        prepared_stage_module._extract_unix_compress(archive, destination, budget)

    assert not destination.exists()


def test_extract_unix_compress_url_patch_requires_gzip(tmp_path, monkeypatch):
    archive = Path(__file__).parent.parent / "data" / "compression" / "Foo.Z"
    destination = tmp_path / "payload"
    budget = prepared_stage_module._PreparationBudget(max_entries=1, max_expanded_bytes=1024)
    monkeypatch.setattr(prepared_stage_module.shutil, "which", lambda executable: None)

    with pytest.raises(PreparedStageError, match="gzip executable is required"):
        prepared_stage_module._extract_unix_compress(archive, destination, budget)

    assert not destination.exists()


@pytest.mark.requires_executables("gzip")
def test_extract_unix_compress_url_patch_rejects_invalid_input(tmp_path):
    archive = tmp_path / "invalid.Z"
    destination = tmp_path / "payload"
    archive.write_bytes(b"not compressed")
    budget = prepared_stage_module._PreparationBudget(max_entries=1, max_expanded_bytes=1024)

    with pytest.raises(PreparedStageError, match="invalid .Z-compressed URL patch"):
        prepared_stage_module._extract_unix_compress(archive, destination, budget)

    assert not destination.exists()


@pytest.mark.requires_executables("gzip")
def test_prepare_stage_rolls_back_malformed_unix_compress_url_patch(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "source.tar.gz"
    patch_archive = tmp_path / "fix.patch.Z"
    fixture = Path(__file__).parent.parent / "data" / "compression" / "Foo.Z"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    patch_archive.write_bytes(fixture.read_bytes())
    plan = source_plan(archive, provenance)
    add_url_patch(
        plan,
        patch_archive.as_uri(),
        hashlib.sha256(b"TEST\n").hexdigest(),
        archive_sha256=hashlib.sha256(patch_archive.read_bytes()).hexdigest(),
        extension="Z",
    )

    with pytest.raises(PreparedStageError, match="invalid URL patch payload"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


@pytest.mark.requires_executables("patch")
def test_prepare_stage_rolls_back_bad_expanded_url_patch_checksum(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "source.tar.gz"
    patch_archive = tmp_path / "fix.tar.gz"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    content = "--- file\n+++ file\n@@ -1 +1 @@\n-before\n+after\n"
    write_tar(patch_archive, [("fix.patch", content, tarfile.REGTYPE)])
    plan = source_plan(archive, provenance)
    add_url_patch(
        plan,
        patch_archive.as_uri(),
        "0" * 64,
        archive_sha256=hashlib.sha256(patch_archive.read_bytes()).hexdigest(),
        extension="tar.gz",
    )

    with pytest.raises(PreparedStageError, match="payload checksum"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


@pytest.mark.requires_executables("patch")
def test_prepare_stage_rolls_back_malformed_url_patch(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    patch_path = tmp_path / "fix.patch"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    content = b"not a unified diff\n"
    patch_path.write_bytes(content)
    plan = source_plan(archive, provenance)
    add_url_patch(plan, patch_path.as_uri(), hashlib.sha256(content).hexdigest())

    with pytest.raises(PreparedStageError, match="invalid URL patch payload"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


@pytest.mark.requires_executables("patch")
def test_prepare_stage_applies_aggregate_url_patch_size_limit(
    tmp_path, provenance, fetch_policy, monkeypatch
):
    archive = tmp_path / "source.tar.gz"
    patch_path = tmp_path / "fix.patch"
    write_tar(archive, [("project/file", "before\n", tarfile.REGTYPE)])
    content = b"--- file\n+++ file\n@@ -1 +1 @@\n-before\n+after\n"
    patch_path.write_bytes(content)
    plan = source_plan(archive, provenance)
    add_url_patch(plan, patch_path.as_uri(), hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(prepared_stage_module, "MAX_PATCH_BYTES_TOTAL", len(content) - 1)

    with pytest.raises(PreparedStageError, match="aggregate size limit"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()


def test_prepare_stage_rejects_resource_conflict_transactionally(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(
        archive,
        [
            ("vendor/headers/existing", "main", tarfile.REGTYPE),
            ("configure", "main", tarfile.REGTYPE),
        ],
    )
    write_tar(resource, [("new", "resource", tarfile.REGTYPE)])
    plan = add_resource(source_plan(archive, provenance), resource)

    with pytest.raises(PreparedStageError, match="placement already exists"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


def test_prepare_stage_applies_aggregate_expanded_size_limit(
    tmp_path, provenance, fetch_policy, monkeypatch
):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(archive, [("main", "123", tarfile.REGTYPE)])
    write_tar(resource, [("resource", "456", tarfile.REGTYPE)])
    plan = add_resource(source_plan(archive, provenance), resource)
    monkeypatch.setattr(prepared_stage_module, "MAX_EXPANDED_BYTES", 5)

    with pytest.raises(PreparedStageError, match="expand beyond"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()


@pytest.mark.parametrize(
    "entries",
    [
        [("ok", "partial", tarfile.REGTYPE), ("../escape", "bad", tarfile.REGTYPE)],
        [("project/link", "../../escape", tarfile.SYMTYPE)],
    ],
)
def test_prepare_stage_rejects_unsafe_tar_entries(tmp_path, provenance, fetch_policy, entries):
    archive = tmp_path / "unsafe.tar.gz"
    write_tar(archive, entries)

    with pytest.raises(PreparedStageError, match="unsafe archive path|unsupported archive entry"):
        prepare_stage(
            source_plan(archive, provenance),
            tmp_path / "prepared",
            expected_provenance=provenance,
            fetch_policy=fetch_policy,
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))
    assert not (tmp_path / "escape").exists()


def test_prepare_stage_rejects_ambiguous_top_level_archive_layout(
    tmp_path, provenance, fetch_policy
):
    archive = tmp_path / "ambiguous.tar.gz"
    write_tar(
        archive,
        [
            ("project/configure", "#!/bin/sh\n", tarfile.REGTYPE),
            (".metadata", "ambiguous", tarfile.REGTYPE),
        ],
    )

    with pytest.raises(PreparedStageError, match="beside top-level directory"):
        prepare_stage(
            source_plan(archive, provenance),
            tmp_path / "prepared",
            expected_provenance=provenance,
            fetch_policy=fetch_policy,
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


def test_prepare_stage_rejects_zip_symlink(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        link = zipfile.ZipInfo("project/link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        output.writestr(link, "../../escape")

    with pytest.raises(PreparedStageError, match="unsupported archive entry"):
        prepare_stage(
            source_plan(archive, provenance, extension="zip"),
            tmp_path / "prepared",
            expected_provenance=provenance,
            fetch_policy=fetch_policy,
        )

    assert not (tmp_path / "prepared").exists()


def test_prepare_stage_rejects_bad_checksum(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    write_tar(archive, [("project/README", "contents", tarfile.REGTYPE)])
    plan = source_plan(archive, provenance)
    plan["source"]["sha256"] = "0" * 64

    with pytest.raises(PreparedStageError, match="checksum"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepared.preparing-*"))


def test_prepare_stage_does_not_replace_existing_path(tmp_path, provenance, fetch_policy):
    archive = tmp_path / "source.tar.gz"
    write_tar(archive, [("project/README", "contents", tarfile.REGTYPE)])
    destination = tmp_path / "prepared"
    destination.mkdir()

    with pytest.raises(PreparedStageError, match="must not already exist"):
        prepare_stage(
            source_plan(archive, provenance),
            destination,
            expected_provenance=provenance,
            fetch_policy=fetch_policy,
        )


def test_prepare_stage_enforces_expanded_size_limit(
    tmp_path, provenance, fetch_policy, monkeypatch
):
    archive = tmp_path / "source.tar.gz"
    write_tar(archive, [("project/data", "too large", tarfile.REGTYPE)])
    monkeypatch.setattr(prepared_stage_module, "MAX_EXPANDED_BYTES", 4)

    with pytest.raises(PreparedStageError, match="size limit"):
        prepare_stage(
            source_plan(archive, provenance),
            tmp_path / "prepared",
            expected_provenance=provenance,
            fetch_policy=fetch_policy,
        )


def test_prepare_stage_enforces_entry_limit(tmp_path, provenance, fetch_policy, monkeypatch):
    archive = tmp_path / "source.tar.gz"
    write_tar(
        archive, [("project/one", "1", tarfile.REGTYPE), ("project/two", "2", tarfile.REGTYPE)]
    )
    monkeypatch.setattr(prepared_stage_module, "MAX_ARCHIVE_ENTRIES", 1)

    with pytest.raises(PreparedStageError, match="too many entries"):
        prepare_stage(
            source_plan(archive, provenance),
            tmp_path / "prepared",
            expected_provenance=provenance,
            fetch_policy=fetch_policy,
        )


def test_prepare_stage_can_publish_unexpanded_source(tmp_path, provenance, fetch_policy):
    source = tmp_path / "script.sh"
    source.write_text("#!/bin/sh\n", encoding="utf-8")

    prepared = prepare_stage(
        source_plan(source, provenance, extension=None, expand=False),
        tmp_path / "prepared",
        expected_provenance=provenance,
        fetch_policy=fetch_policy,
    )

    assert (prepared.path / "script.sh").read_text(encoding="utf-8") == "#!/bin/sh\n"


def test_prepared_stage_digest_detects_content_changes(tmp_path):
    root = tmp_path / "prepared"
    root.mkdir()
    source = root / "source"
    source.write_text("before", encoding="utf-8")
    before = prepared_stage_digest(root)

    source.write_text("after", encoding="utf-8")

    assert prepared_stage_digest(root) != before


def test_prepared_stage_digest_rejects_links(tmp_path):
    root = tmp_path / "prepared"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(PreparedStageError, match="unsupported prepared-stage entry"):
        prepared_stage_digest(root)

    root_link = tmp_path / "prepared-link"
    root_link.symlink_to(root, target_is_directory=True)
    with pytest.raises(PreparedStageError, match="root must be a directory"):
        prepared_stage_digest(root_link)


def test_prepare_stage_rejects_file_outside_allowed_roots(tmp_path, provenance):
    source = tmp_path / "script.sh"
    source.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(PreparedStageError, match="outside allowed file roots"):
        prepare_stage(
            source_plan(source, provenance, extension=None, expand=False),
            tmp_path / "prepared",
            expected_provenance=provenance,
            fetch_policy=SourceFetchPolicy(file_roots=(tmp_path / "other",)),
        )


def test_prepare_stage_authorizes_all_candidates_before_fetch(
    tmp_path, provenance, fetch_policy, monkeypatch
):
    source = tmp_path / "script.sh"
    source.write_text("#!/bin/sh\n", encoding="utf-8")
    plan = source_plan(source, provenance, extension=None, expand=False)
    plan["source"]["urls"].append("https://unauthorized.example/source")
    opened = False
    original_open = urllib.request.OpenerDirector.open

    def record_open(self, *args, **kwargs):
        nonlocal opened
        opened = True
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", record_open)
    with pytest.raises(PreparedStageError, match="authority is not allowed"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not opened


def test_prepare_stage_authorizes_resources_before_any_fetch(
    tmp_path, provenance, fetch_policy, monkeypatch
):
    archive = tmp_path / "source.tar.gz"
    resource = tmp_path / "headers.tar.gz"
    write_tar(archive, [("main", "source", tarfile.REGTYPE)])
    write_tar(resource, [("header", "resource", tarfile.REGTYPE)])
    plan = add_resource(source_plan(archive, provenance), resource)
    plan["resources"][0]["source"]["urls"] = ["https://unauthorized.example/headers"]
    opened = False
    original_open = urllib.request.OpenerDirector.open

    def record_open(self, *args, **kwargs):
        nonlocal opened
        opened = True
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", record_open)
    with pytest.raises(PreparedStageError, match="authority is not allowed"):
        prepare_stage(
            plan, tmp_path / "prepared", expected_provenance=provenance, fetch_policy=fetch_policy
        )

    assert not opened


def test_prepare_stage_rejects_redirect_before_target_request(tmp_path, provenance):
    class TargetHandler(http.server.BaseHTTPRequestHandler):
        requests = 0

        def do_GET(self):
            type(self).requests += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"source")

        def log_message(self, format, *args):
            pass

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        location = ""

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", type(self).location)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    redirect = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    RedirectHandler.location = f"http://127.0.0.1:{target.server_port}/source"
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (target, redirect)
    ]
    for thread in threads:
        thread.start()
    source = tmp_path / "source"
    source.write_bytes(b"source")
    plan = source_plan(source, provenance, extension=None, expand=False)
    plan["source"]["urls"] = [f"http://127.0.0.1:{redirect.server_port}/redirect"]
    try:
        with pytest.raises(PreparedStageError, match="authority is not allowed"):
            prepare_stage(
                plan,
                tmp_path / "prepared",
                expected_provenance=provenance,
                fetch_policy=SourceFetchPolicy(
                    http_origins=frozenset({("127.0.0.1", redirect.server_port)})
                ),
            )
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()

    assert TargetHandler.requests == 0
