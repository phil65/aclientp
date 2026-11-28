"""PEP 810 ``__lazy_modules__`` in the package ``__init__``."""

import subprocess
import sys

import pytest

import aclientp


def test_all_public_exports_resolve() -> None:
    for name in aclientp.__all__:
        assert getattr(aclientp, name) is not None, f"aclientp.{name} failed to resolve"


@pytest.mark.skipif(sys.version_info < (3, 15), reason="PEP 810 __lazy_modules__ needs 3.15+")
def test_schema_import_skips_clients_and_transports() -> None:
    code = (
        "import sys\n"
        "import aclientp.schema\n"
        "for name in ('aclientp.client', 'aclientp.transports', 'aclientp.stdio', 'anyenv'):\n"
        "    assert name not in sys.modules, name\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
