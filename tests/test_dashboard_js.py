"""Corre los tests de JS del dashboard (tests/js/, node --test) desde pytest.
Se saltean si no hay node: en la Pi no hace falta."""
import os
import pathlib
import shutil
import subprocess

import pytest

JS_TESTS = sorted((pathlib.Path(__file__).parent / "js").glob("test_*.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node no instalado")
@pytest.mark.parametrize("tz", ["UTC", "America/Argentina/Buenos_Aires", "Asia/Tokyo"])
@pytest.mark.parametrize("path", JS_TESTS, ids=lambda p: p.name)
def test_js(path, tz):
    """En varias zonas: el navegador del dashboard no tiene por qué estar en la de la Pi."""
    env = {**os.environ, "TZ": tz}
    r = subprocess.run(["node", "--test", str(path)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
