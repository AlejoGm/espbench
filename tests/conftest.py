"""Aislamiento global: ningún test toca /opt/esp de la máquina donde corre
(en la Pi, leería el estado y los logs de los devices reales)."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_esp_base(monkeypatch, tmp_path):
    monkeypatch.setenv("ESP_BASE", str(tmp_path / "_esp_base"))
