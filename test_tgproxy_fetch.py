"""Тесты fetch_all и alive() с подменой сети: urlopen и check_proxy не зовутся по-настоящему."""
import urllib.parse as ps
from urllib.error import URLError

import tgproxy_gui as g

HEX32 = "a" * 32


def u(server, port, secret=HEX32):
    return f"https://t.me/proxy?server={server}&port={port}&secret={secret}"


class FakeResponse:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeResponse_raw:
    """Ответ сырыми байтами (для тестов битого UTF-8)."""
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------- fetch_all: дедуп, битые строки, упавший источник ----------

def test_fetch_all_dedup_bad_and_fail(monkeypatch):
    """Один источник отдаёт строки (дубликат + битая + валидная), второй - URLError."""
    text = "\n".join([u("h1", 443), u("h1", 443), "garbage", u("h2", 8443)])
    seen_urls = []

    def fake_urlopen(url, timeout):
        seen_urls.append(url)
        if "zakky8" in url:
            return FakeResponse(text)
        raise URLError("сеть недоступна")

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    proxies, bad, fails, dups = g.fetch_all({"zakky8": g.PRESETS["zakky8"], "dead": "http://x/"})

    assert len(seen_urls) == 2  # обошли оба источника, несмотря на падение первого
    assert [(s, p) for _, s, p in proxies] == [("h1", 443), ("h2", 8443)]
    assert dups == 1
    assert bad == 1
    assert fails == [("dead", "<urlopen error сеть недоступна>")]


def test_fetch_all_bom_and_empty(monkeypatch):
    """BOM в начале файла срезается; полностью пустой ответ даёт пустой список."""
    def fake_urlopen(url, timeout):
        return FakeResponse("\ufeff" + u("h1", 443) if "zakky8" in url else "")

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    proxies, bad, fails, dups = g.fetch_all(g.PRESETS)
    assert [(s, p) for _, s, p in proxies] == [("h1", 443)]
    assert bad == 0 and fails == [] and dups == 0


def test_fetch_all_server_dot_normalization(monkeypatch):
    """server с точкой на конце и без: дедуп по нормализованному хосту."""
    def fake_urlopen(url, timeout):
        return FakeResponse(u("host.name.", 443) + "\n" + u("host.name", 443))

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    proxies, bad, fails, dups = g.fetch_all({"zakky8": g.PRESETS["zakky8"]})
    assert len(proxies) == 1 and dups == 1


def test_fetch_all_invalid_utf8_replaced(monkeypatch):
    """Битый UTF-8 в ответе источника не валит источник: строки декодируются с replace."""
    def fake_urlopen(url, timeout):
        return FakeResponse_raw(b"\xff\xfe " + u("h1", 443).encode("utf-8"))

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    proxies, bad, fails, dups = g.fetch_all({"zakky8": g.PRESETS["zakky8"]})
    assert fails == []  # источник не упал
    assert [(s, p) for _, s, p in proxies] == [("h1", 443)]


# ---------- alive: хендшейк ----------

def test_alive_true(monkeypatch):
    class R:
        ok = True

    monkeypatch.setattr(g, "check_proxy", lambda url, opts: R())
    assert g.alive("https://t.me/proxy?server=x&port=443&secret=" + HEX32) is True


def test_alive_false_on_check_fail(monkeypatch):
    class R:
        ok = False

    monkeypatch.setattr(g, "check_proxy", lambda url, opts: R())
    assert g.alive("https://t.me/proxy?server=x&port=443&secret=" + HEX32) is False


def test_alive_false_on_exception(monkeypatch):
    def boom(url, opts):
        raise RuntimeError("таймаут")

    monkeypatch.setattr(g, "check_proxy", boom)
    assert g.alive("https://t.me/proxy?server=x&port=443&secret=" + HEX32) is False


def test_alive_passes_connect_opts(monkeypatch):
    """alive() передаёт глобальные OPTS (connect/response timeout)."""
    got = {}

    def spy(url, opts):
        got["opts"] = opts

        class R:
            ok = True

        return R()

    monkeypatch.setattr(g, "check_proxy", spy)
    g.alive(u("x", 443))
    assert got["opts"] is g.OPTS
