"""Тесты ci/set_version.py: внедрение версии из тега в APP_VERSION (TDD).

Скрипт запускается и из workflow, и здесь - загрузкой модуля по пути:
ci/ не пакет, обычным import его не достать.
"""
import importlib.util
import pathlib
import sys

import pytest

CI = pathlib.Path(__file__).resolve().parent / "ci" / "set_version.py"


def load_module():
    spec = importlib.util.spec_from_file_location("set_version_under_test", CI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return load_module()


def write_app(tmp_path, text):
    f = tmp_path / "app.py"
    f.write_text(text, encoding="utf-8")
    return f


def test_replaces_value(mod, tmp_path):
    f = write_app(tmp_path, 'APP_VERSION = "1.0.5"\nprint(APP_VERSION)\n')
    assert mod.set_version(f, "9.9.9") == 0
    assert 'APP_VERSION = "9.9.9"' in f.read_text(encoding="utf-8")


def test_not_found_returns_1_and_keeps_file(mod, tmp_path, capsys):
    f = write_app(tmp_path, "x = 1\n")
    assert mod.set_version(f, "9.9.9") == 1
    assert "ожидалось ровно 1" in capsys.readouterr().out
    assert f.read_text(encoding="utf-8") == "x = 1\n"  # файл не тронут


def test_multiple_matches_returns_1(mod, tmp_path, capsys):
    f = write_app(tmp_path, 'APP_VERSION = "1.0.5"\nAPP_VERSION = "1.0.6"\n')
    assert mod.set_version(f, "9.9.9") == 1
    assert "совпадений APP_VERSION: 2" in capsys.readouterr().out


def test_bad_version_value_returns_1(mod, tmp_path):
    f = write_app(tmp_path, 'APP_VERSION = "1.0.5"\n')
    assert mod.set_version(f, '9"9') == 1
    assert 'APP_VERSION = "1.0.5"' in f.read_text(encoding="utf-8")


def test_crlf_checkout_is_handled(mod, tmp_path):
    """CRLF-чек-аут (autocrlf): константа находится, переводы строк не портятся."""
    f = tmp_path / "app.py"
    f.write_bytes(b'APP_VERSION = "1.0.5"\r\nx = 1\r\n')
    assert mod.set_version(f, "9.9.9") == 0
    assert f.read_bytes() == b'APP_VERSION = "9.9.9"\r\nx = 1\r\n'


def test_main_strips_v_and_writes(mod, tmp_path, monkeypatch):
    f = write_app(tmp_path, 'APP_VERSION = "1.0.5"\n')
    monkeypatch.setattr(mod, "APP", f)
    monkeypatch.setattr(sys, "argv", ["set_version.py", "v9.9.9"])
    assert mod.main() == 0
    assert 'APP_VERSION = "9.9.9"' in f.read_text(encoding="utf-8")


def test_main_without_args_returns_1(mod, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["set_version.py"])
    assert mod.main() == 1
    assert "использование" in capsys.readouterr().out


def test_repo_app_has_exactly_one_app_version(mod):
    """В живом tgproxy_gui.py константа ровно одна, иначе релизный шаг упадёт."""
    assert mod.APP.name == "tgproxy_gui.py"
    assert len(mod.RX.findall(mod.APP.read_text(encoding="utf-8"))) == 1
