"""Aislamiento global: ningún test toca /opt/esp de la máquina donde corre
(en la Pi, leería el estado y los logs de los devices reales), ni sale a buscar
benches a la tailnet real (el CLI sin host hace discovery)."""
import json

import pytest


@pytest.fixture(autouse=True)
def _isolated_esp_base(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path / "_esp_base"))
    benches_cfg = tmp_path / "_benches.json"
    benches_cfg.write_text(json.dumps({"tailscale": False, "hosts": []}))
    monkeypatch.setenv("ESPBENCH_BENCHES_CONFIG", str(benches_cfg))
    monkeypatch.delenv("ESPBENCH_HOST", raising=False)
