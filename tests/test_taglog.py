"""
Tests para taglog: logging con TAG + timestamp, sinks pluggables.
"""
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "remote"))

from server import taglog


@pytest.fixture(autouse=True)
def _reset_sinks():
    """Cada test arranca y termina con el estado default (solo stdout)."""
    taglog.reset_default_sinks()
    yield
    taglog.reset_default_sinks()


def _capture():
    calls = []
    taglog.add_sink(lambda ts, level, tag, msg: calls.append((ts, level, tag, msg)))
    return calls


def test_info_reaches_sink_with_level_and_tag():
    calls = _capture()
    taglog.info("protocol", "hola")
    assert len(calls) == 1
    _, level, tag, msg = calls[0]
    assert level == "INFO"
    assert tag == "protocol"
    assert msg == "hola"


def test_warn_error_debug_levels():
    calls = _capture()
    taglog.warn("monitor", "w")
    taglog.error("flash", "e")
    taglog.debug("device", "d")
    levels = [c[1] for c in calls]
    assert levels == ["WARN", "ERROR", "DEBUG"]


def test_timestamp_format():
    calls = _capture()
    taglog.info("x", "y")
    ts = calls[0][0]
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", ts)


def test_multiple_sinks_all_fire():
    calls_a, calls_b = [], []
    taglog.add_sink(lambda ts, l, t, m: calls_a.append(m))
    taglog.add_sink(lambda ts, l, t, m: calls_b.append(m))
    taglog.info("x", "hola")
    assert calls_a == ["hola"]
    assert calls_b == ["hola"]


def test_broken_sink_does_not_block_others():
    calls = []

    def bad_sink(ts, level, tag, msg):
        raise RuntimeError("sink roto")

    taglog.add_sink(bad_sink)
    taglog.add_sink(lambda ts, l, t, m: calls.append(m))
    taglog.info("x", "sigue andando")

    assert calls == ["sigue andando"]


def test_clear_sinks_silences_everything():
    calls = _capture()
    taglog.clear_sinks()
    taglog.info("x", "no debería llegar")
    assert calls == []


def test_reset_default_sinks_drops_extra_sinks():
    calls = _capture()
    taglog.reset_default_sinks()
    taglog.info("x", "no debería llegar al sink viejo")
    assert calls == []


def test_default_stdout_sink_prints(capsys):
    taglog.info("protocol", "mensaje de prueba")
    out = capsys.readouterr().out
    assert "INFO" in out
    assert "protocol" in out
    assert "mensaje de prueba" in out
