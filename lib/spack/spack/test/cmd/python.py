# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import io
import platform
import sys

import pytest

import spack
from spack.main import SpackCommand

python = SpackCommand("python")


def test_python(monkeypatch, tmp_path):
    """
    Test that the python command can import Spack and print the Spack version.

    Use python -c to verify it runs only the given command without executing
    the PYTHONSTARTUP startup file.
    """
    startup_file = tmp_path / "startup.py"
    startup_file.write_text("print('startup script should not run with -c')", encoding="utf-8")
    monkeypatch.setenv("PYTHONSTARTUP", str(startup_file))

    out = python("-c", "import spack; print(spack.spack_version)")
    assert out.strip() == spack.spack_version


def test_python_interactive_runs_startup(monkeypatch, tmp_path):
    """
    Test that the python command can run an interactive python interpreter, runs
    the PYTHONSTARTUP startup file early before interactive mode as expected, and
    that it can import Spack and print the Spack version of the imported module.
    """
    startup_file = tmp_path / "startup.py"
    startup_mesg = "startup script ran"
    startup_file.write_text(f"print('{startup_mesg}')", encoding="utf-8")
    monkeypatch.setenv("PYTHONSTARTUP", str(startup_file))
    monkeypatch.setattr(sys, "stdin", io.StringIO("import spack; print(spack.spack_version)"))

    out = python().strip()
    assert out.startswith(startup_mesg)
    assert f"Spack version {spack.spack_version}" in out
    assert f">>> {spack.spack_version}" in out


def test_python_interpreter_path():
    out = python("--path")
    assert out.strip() == sys.executable


def test_python_version():
    out = python("-V")
    assert platform.python_version() in out


def test_python_with_module():
    # pytest rewrites a lot of modules, which interferes with runpy, so
    # it's hard to test this.  Trying to import a module like sys, that
    # has no code associated with it, raises an error reliably in python
    # 2 and 3, which indicates we successfully ran runpy.run_module.
    with pytest.raises(ImportError, match="No code object"):
        python("-m", "sys")


def test_python_raises():
    out = python("--foobar", fail_on_error=False)
    assert python.returncode == 2
    assert "--foobar" in out
