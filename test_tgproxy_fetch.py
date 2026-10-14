"""Тесты fetch_all и alive() с подменой сети: urlopen и check_proxy не зовутся по-настоящему."""
import urllib.parse as ps
from urllib.error import HTTPError, URLError

import tgproxy_gui as g

HEX32 = "a" * 32


def u(server, port, secret=HEX32):
    return f"https://t.me/proxy?server={server}&port={port}&secret={secret}"


class FakeResponse:
    def __init__(self, text):
        self._text = text

    def read(self, limit=-1):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeResponse_raw:
    """Ответ сырыми байтами (для тестов битого UTF-8)."""
    def __init__(self, data):
        self._data = data

    def read(self, limit=-1):
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

    def fake_urlopen(req, timeout):
        url = getattr(req, "full_url", req)
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
    def fake_urlopen(req, timeout):
        return FakeResponse("\ufeff" + u("h1", 443) if "zakky8" in getattr(req, "full_url", req) else "")

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    proxies, bad, fails, dups = g.fetch_all(g.PRESETS)
    assert [(s, p) for _, s, p in proxies] == [("h1", 443)]
    assert bad == 0 and fails == [] and dups == 0


def test_fetch_all_server_dot_normalization(monkeypatch):
    """server с точкой на конце и без: дедуп по нормализованному хосту."""
    def fake_urlopen(req, timeout):
        return FakeResponse(u("host.name.", 443) + "\n" + u("host.name", 443))

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    proxies, bad, fails, dups = g.fetch_all({"zakky8": g.PRESETS["zakky8"]})
    assert len(proxies) == 1 and dups == 1


def test_fetch_all_invalid_utf8_replaced(monkeypatch):
    """Битый UTF-8 в ответе источника не валит источник: строки декодируются с replace."""
    def fake_urlopen(req, timeout):
        return FakeResponse_raw(b"\xff\xfe " + u("h1", 443).encode("utf-8"))

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    proxies, bad, fails, dups = g.fetch_all({"zakky8": g.PRESETS["zakky8"]})
    assert fails == []  # источник не упал
    assert [(s, p) for _, s, p in proxies] == [("h1", 443)]


# ---------- alive: двухфазная проверка ----------

def test_alive_true(monkeypatch):
    class R:
        ok = True

    monkeypatch.setattr(g, "check_proxy", lambda url, opts: R())
    monkeypatch.setattr(g.socket, "create_connection", lambda addr, timeout: _NoopSock())
    assert g.alive("https://t.me/proxy?server=x&port=443&secret=" + HEX32) is True


def test_alive_false_on_check_fail(monkeypatch):
    class R:
        ok = False

    monkeypatch.setattr(g, "check_proxy", lambda url, opts: R())
    monkeypatch.setattr(g.socket, "create_connection", lambda addr, timeout: _NoopSock())
    assert g.alive("https://t.me/proxy?server=x&port=443&secret=" + HEX32) is False


def test_alive_false_on_exception(monkeypatch):
    def boom(url, opts):
        raise RuntimeError("таймаут")

    monkeypatch.setattr(g, "check_proxy", boom)
    monkeypatch.setattr(g.socket, "create_connection", lambda addr, timeout: _NoopSock())
    assert g.alive("https://t.me/proxy?server=x&port=443&secret=" + HEX32) is False


def test_alive_tcp_fail_skips_handshake(monkeypatch):
    """Фаза 1: TCP не открылся - check_proxy не зовётся вовсе (экономия 5с на мёртвом)."""
    def no_connect(addr, timeout):
        raise OSError("недоступен")

    called = []
    monkeypatch.setattr(g, "check_proxy", lambda url, opts: called.append(url))
    monkeypatch.setattr(g.socket, "create_connection", no_connect)
    assert g.alive("https://t.me/proxy?server=x&port=443&secret=" + HEX32) is False
    assert called == []  # хендшейк пропущен


def test_alive_tcp_timeout_value(monkeypatch):
    """Фаза 1 использует короткий таймаут 2.5с, а не глобальные OPTS."""
    seen = {}

    def spy_connect(addr, timeout):
        seen["addr"], seen["timeout"] = addr, timeout
        raise OSError("всё равно падаем")

    monkeypatch.setattr(g.socket, "create_connection", spy_connect)
    g.alive(u("h", 443))
    assert seen["addr"] == ("h", 443)
    assert seen["timeout"] == 2.5


def test_alive_passes_connect_opts(monkeypatch):
    """Фаза 2 передаёт глобальные OPTS (connect/response timeout)."""
    got = {}

    def spy(url, opts):
        got["opts"] = opts

        class R:
            ok = True

        return R()

    monkeypatch.setattr(g, "check_proxy", spy)
    monkeypatch.setattr(g.socket, "create_connection", lambda addr, timeout: _NoopSock())
    g.alive(u("x", 443))
    assert got["opts"] is g.OPTS


class _NoopSock:
    """Заглушка TCP-сокета для with-блока."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------- проверка обновлений: сетевой слой под моками urllib ----------

def test_check_for_updates_newer_and_request_shape(monkeypatch):
    """GET к GitHub Releases: URL, обязательные заголовки и timeout 10 секунд."""
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return FakeResponse('{"tag_name": "v9.9.9"}')

    monkeypatch.setattr(g.rq, "urlopen", fake_urlopen)
    assert g.check_for_updates() == ("newer", "v9.9.9")
    assert seen["url"] == "https://api.github.com/repos/akenra/tgproxy/releases/latest"
    assert seen["timeout"] == 10
    assert seen["headers"]["user-agent"] == "tgproxy/" + g.APP_VERSION
    assert seen["headers"]["accept"] == "application/vnd.github+json"


def test_check_for_updates_equal_older_and_junk(monkeypatch):
    """Тот же тег, старее и мусорный tag_name: всё «не новее», без ошибки."""
    for payload, expected in (
        ('{"tag_name": "v' + g.APP_VERSION + '"}', ("current", None)),
        ('{"tag_name": "v0.0.1"}', ("current", None)),
        ('{"tag_name": "v1.0.6-beta"}', ("current", None)),
        ('{"tag_name": null}', ("current", None)),
        ("{}", ("current", None)),
    ):
        monkeypatch.setattr(g.rq, "urlopen",
                            lambda req, timeout, _p=payload: FakeResponse(_p))
        assert g.check_for_updates() == expected, payload


def test_check_for_updates_errors_are_uniform(monkeypatch):
    """Сеть, статус != 200 и битый JSON - одинаково («error», None); без ретраев."""
    calls = []

    def net(req, timeout):
        calls.append(1)
        raise URLError("сеть недоступна")

    monkeypatch.setattr(g.rq, "urlopen", net)
    assert g.check_for_updates() == ("error", None)
    assert calls == [1]

    def status(req, timeout):
        raise HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(g.rq, "urlopen", status)
    assert g.check_for_updates() == ("error", None)

    monkeypatch.setattr(g.rq, "urlopen", lambda req, timeout: FakeResponse("{битый json"))
    assert g.check_for_updates() == ("error", None)


def test_check_for_updates_request_construction_failure(monkeypatch):
    """Сбой построения Request (правка UPDATE_API, экзотическое окружение) - тоже
    («error», None): поток-воркер не умирает, кнопка не залипает в disabled."""
    def boom(*a, **kw):
        raise ValueError("битый URL")

    monkeypatch.setattr(g.rq, "Request", boom)
    assert g.check_for_updates() == ("error", None)
