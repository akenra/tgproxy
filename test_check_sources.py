"""Тесты ci/check_sources.py: переходы состояния, алерт и JSON (TDD).

Скрипт вне пакета - грузится по пути, как ci/set_version.py в test_set_version.py.
Сеть запрещена: urlopen подменяется фейком, run() принимает фейковый fetch.
"""
import importlib.util
import json
import os
import pathlib
import re

import pytest

CI = pathlib.Path(__file__).resolve().parent / "ci" / "check_sources.py"
TODAY = "2026-10-14"
SECRET = "0123456789abcdef0123456789abcdef"
URL_A = f"https://t.me/proxy?server=a.example.com&port=443&secret={SECRET}"


def load_module():
    spec = importlib.util.spec_from_file_location("check_sources_under_test", CI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return load_module()


class FakeResponse:
    """urlopen-ответ: контекстный менеджер с read() как у HTTPResponse."""

    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data[:n] if n >= 0 else self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_fetch(status, count=None, nbytes=None, error=None):
    """Фейковый fetch_source: сеть не нужна."""
    return lambda url: {"status": status, "count": count, "bytes": nbytes, "error": error}


def silent(*args):
    pass


OK = {"status": "ok", "count": 7, "bytes": 100, "error": None}
EMPTY = {"status": "empty", "count": 0, "bytes": 10, "error": None}
ERROR = {"status": "error", "count": None, "bytes": None, "error": "timed out"}


def test_evaluate_ok_resets_streak_and_error(mod):
    prev = {"last_ok": "2026-10-01", "last_count": 4, "empty_streak": 2, "last_error": "timeout"}
    new = mod.evaluate(prev, OK, TODAY)
    assert new == {"last_ok": TODAY, "last_count": 7, "empty_streak": 0, "last_error": None}
    assert prev["empty_streak"] == 2 and prev["last_error"] == "timeout"  # prev не мутирован


def test_evaluate_empty_increments_and_zeroes_count(mod):
    prev = {"last_ok": "2026-10-01", "last_count": 4, "empty_streak": 1, "last_error": None}
    new = mod.evaluate(prev, EMPTY, TODAY)
    assert new["empty_streak"] == 2
    assert new["last_count"] == 0
    assert new["last_ok"] == "2026-10-01"  # пустота last_ok не трогает


def test_evaluate_error_sets_last_error_only(mod):
    prev = {"last_ok": "2026-10-01", "last_count": 4, "empty_streak": 2, "last_error": None}
    new = mod.evaluate(prev, ERROR, TODAY)
    assert new == {"last_ok": "2026-10-01", "last_count": 4, "empty_streak": 2,
                   "last_error": "timed out"}


def test_evaluate_fresh_source_gets_full_shape(mod):
    new = mod.evaluate(None, ERROR, TODAY)
    assert new == {"last_ok": None, "last_count": None, "empty_streak": 0,
                   "last_error": "timed out"}


def test_exit_code_alerts_on_error_or_long_streak(mod):
    assert mod.exit_code({"a": {"status": "ok"}}, {"a": {"empty_streak": 2}}) == 0
    assert mod.exit_code({"a": {"status": "empty"}}, {"a": {"empty_streak": 2}}) == 0
    assert mod.exit_code({"a": {"status": "error"}}, {"a": {"empty_streak": 0}}) == 1
    assert mod.exit_code({"a": {"status": "empty"}}, {"a": {"empty_streak": 3}}) == 1


def test_fetch_source_ok_counts_unique_valid_and_pins_request(mod, monkeypatch):
    raw = ("# шапка списка\n"
           f"{URL_A}\n"
           "https://t.me/proxy?server=b.example.com&port=8443&"
           f"secret=fedcba9876543210fedcba9876543210\n"
           f"{URL_A}\n"  # дубль - в счёт не идёт
           "мусор\n").encode("utf-8")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["ua"] = req.headers.get("User-agent")
        seen["timeout"] = timeout
        seen["read_n"] = None
        resp = FakeResponse(raw)
        orig = resp.read

        def read(n=-1):
            seen["read_n"] = n
            return orig(n)

        resp.read = read
        return resp

    monkeypatch.setattr(mod.rq, "urlopen", fake_urlopen)
    r = mod.fetch_source("https://example.com/list.txt")
    assert r == {"status": "ok", "count": 2, "bytes": len(raw), "error": None}
    assert seen == {"url": "https://example.com/list.txt", "ua": "tgproxy/1.0",
                    "timeout": 30, "read_n": 5 * 1024 * 1024}


def test_fetch_source_empty_when_only_comments(mod, monkeypatch):
    monkeypatch.setattr(mod.rq, "urlopen",
                        lambda req, timeout=None: FakeResponse(b"# only comments\n\n"))
    assert mod.fetch_source("https://example.com/list.txt") == {
        "status": "empty", "count": 0, "bytes": 17, "error": None}


def test_fetch_source_error_is_short_and_safe(mod, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused " * 30)

    monkeypatch.setattr(mod.rq, "urlopen", boom)
    r = mod.fetch_source("https://example.com/list.txt")
    assert r["status"] == "error"
    assert r["count"] is None and r["bytes"] is None
    assert r["error"].startswith("connection refused")
    assert len(r["error"]) <= 120 and "\n" not in r["error"]


def test_render_table_shows_all_columns(mod):
    table = mod.render_table({
        "b": {"status": "ok", "count": 3, "bytes": 100, "error": None},
        "a": {"status": "error", "count": None, "bytes": None, "error": "timed out"},
    })
    lines = table.splitlines()
    assert lines[0].split() == ["источник", "статус", "валидных", "байт", "ошибка"]
    assert lines[1].split() == ["a", "error", "-", "-", "timed", "out"]
    assert lines[2].split() == ["b", "ok", "3", "100"]
    assert table.index("a ") < table.index("b ")  # источники по алфавиту


def test_run_streak_three_empties_alerts(mod, tmp_path):
    path = tmp_path / "sources-status.json"
    empty = fake_fetch("empty", count=0, nbytes=10)
    for _ in range(2):
        assert mod.run(fetch=empty, today=TODAY, path=path, out=silent) == 0
    state = mod.load_state(path)
    assert set(state) == set(mod.PRESETS)
    assert all(s["empty_streak"] == 2 for s in state.values())
    assert mod.run(fetch=empty, today=TODAY, path=path, out=silent) == 1
    assert all(s["empty_streak"] == 3 for s in mod.load_state(path).values())


def test_run_error_alert_keeps_last_ok_and_streak(mod, tmp_path):
    path = tmp_path / "sources-status.json"
    assert mod.run(fetch=fake_fetch("ok", count=5, nbytes=123), today=TODAY,
                   path=path, out=silent) == 0
    out = []
    assert mod.run(fetch=fake_fetch("error", error="Connection refused"),
                   today="2026-10-21", path=path, out=out.append) == 1
    assert "Connection refused" in "".join(out)  # ошибка видна в таблице
    state = mod.load_state(path)
    assert all(s == {"last_ok": TODAY, "last_count": 5, "empty_streak": 0,
                     "last_error": "Connection refused"} for s in state.values())


def test_run_writes_state_for_all_sources_with_utc_today(mod, tmp_path):
    path = tmp_path / "sources-status.json"
    assert mod.run(fetch=fake_fetch("ok", count=1, nbytes=10), path=path, out=silent) == 0
    state = mod.load_state(path)
    assert set(state) == set(mod.PRESETS)
    for s in state.values():
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", s["last_ok"])
        assert s["last_count"] == 1 and s["empty_streak"] == 0 and s["last_error"] is None


def test_run_second_pass_same_state_does_not_rewrite(mod, tmp_path):
    path = tmp_path / "sources-status.json"
    ok = fake_fetch("ok", count=1, nbytes=10)
    mod.run(fetch=ok, today=TODAY, path=path, out=silent)
    before = path.read_bytes()
    os.utime(path, (1_000_000_000, 1_000_000_000))
    mod.run(fetch=ok, today=TODAY, path=path, out=silent)
    assert path.read_bytes() == before
    assert path.stat().st_mtime == 1_000_000_000  # файл не перезаписан


def test_run_crlf_state_file_is_not_rewritten(mod, tmp_path):
    """Windows-чек-аут (autocrlf) не считаем изменением: файл остаётся как есть."""
    path = tmp_path / "sources-status.json"
    ok = fake_fetch("ok", count=1, nbytes=10)
    mod.run(fetch=ok, today=TODAY, path=path, out=silent)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    os.utime(path, (1_000_000_000, 1_000_000_000))
    mod.run(fetch=ok, today=TODAY, path=path, out=silent)
    assert b"\r\n" in path.read_bytes()
    assert path.stat().st_mtime == 1_000_000_000


def test_load_state_broken_file_is_empty(mod, tmp_path):
    assert mod.load_state(tmp_path / "нет-файла.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{не json", encoding="utf-8")
    assert mod.load_state(bad) == {}
    bad.write_text("[1, 2]", encoding="utf-8")
    assert mod.load_state(bad) == {}


def test_save_state_format_and_skip(mod, tmp_path):
    path = tmp_path / "sources-status.json"
    state = {"src": {"last_ok": TODAY, "last_count": 1, "empty_streak": 0,
                     "last_error": "ошибка сети"}}
    assert mod.save_state(path, state) is True
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    text = raw.decode("utf-8")
    assert json.loads(text) == state
    assert "ошибка сети" in text  # ensure_ascii=False
    assert text.index('"empty_streak"') < text.index('"last_count"')  # sort_keys
    assert mod.save_state(path, state) is False  # контент тот же - не переписываем
