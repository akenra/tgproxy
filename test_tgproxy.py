"""Спека-тесты для tgproxy_gui: чистые функции parse/valid/fetch/dedup.

Сеть не трогаем: fetch покрыт через parse+valid на живых строках-примерах.
Запуск: python -m pytest test_tgproxy.py -q
"""
import urllib.parse as ps

import tgproxy_gui as g


def u(server, port, secret):
    return f"https://t.me/proxy?server={server}&port={port}&secret={secret}"


HEX32 = "a" * 32
HEX64 = "dd" + "b" * 62  # dd-префикс + hex, итого 64
B64 = "eeNEgYdJvXrFGRMCIMJdCQ"  # реальный пример из живого списка (faketls ee)
B64DD = "dd10400103324995"  # dd в base64-написании, живой список


# ---------- parse ----------

def test_parse_returns_url_server_port():
    line = u("x.y.info.", 7799, HEX32)
    assert g.parse(line) == (line, "x.y.info", 7799)  # точка в конце server срезается


def test_parse_keeps_full_url():
    line = u("1.2.3.4", 443, HEX32)
    url, server, port = g.parse(line)
    assert url == line and server == "1.2.3.4" and port == 443


def test_parse_bad_port_raises():
    try:
        g.parse(u("x", "abc", HEX32))
    except Exception:
        return
    raise AssertionError("parse('port=abc') должен падать")


# ---------- valid: порт ----------

def test_valid_port_bounds():
    assert g.valid(u("x", 1, HEX32))
    assert g.valid(u("x", 65535, HEX32))
    assert not g.valid(u("x", 0, HEX32))
    assert not g.valid(u("x", 65536, HEX32))
    assert not g.valid(u("x", 99999, HEX32))  # регресс: hex-ветка раньше шла до проверки порта


def test_valid_non_numeric_port():
    assert not g.valid(u("x", "abc", HEX32))


# ---------- valid: секрет hex ----------

def test_valid_hex32():
    assert g.valid(u("x", 443, HEX32))


def test_valid_hex64_with_prefix():
    assert g.valid(u("x", 443, HEX64))


def test_valid_hex_wrong_length():
    # нечётная длина из hex-символов это битый hex, не base64
    assert not g.valid(u("x", 443, "a" * 31))
    assert not g.valid(u("x", 443, "a" * 33))


def test_valid_hex_non_hex_chars():
    # 'z'*32 декодится как base64url (24 байта): формат допустим, живость решит alive()
    assert g.valid(u("x", 443, "z" * 32))


# ---------- valid: секрет base64url ----------

def test_valid_base64url_live_samples():
    # реальные строки из публичного списка, раньше отсекались hex-only правилом
    assert g.valid(u("good.cipservice.co.uk", 2083, B64))
    assert g.valid(u("bookmook.hichibehichi-co.info.", 2882, B64DD))


def test_valid_base64url_min_bytes():
    assert g.valid(u("x", 443, "d" * 22))  # 16 байт после декода
    assert not g.valid(u("x", 443, "d" * 11))  # 8 байт: слишком короткий
    assert g.valid(u("x", 443, "dd10400103324995"))  # 12 байт, живой dd-секрет из списка


def test_valid_base64url_garbage_rejected():
    assert not g.valid(u("x", 443, "####"))
    assert not g.valid(u("x", 443, "!!!"))


# ---------- valid: прочее ----------

def test_valid_missing_fields():
    assert not g.valid("https://t.me/proxy?server=x&port=443")  # нет secret
    assert not g.valid("https://t.me/proxy?port=443&secret=" + HEX32)  # нет server
    assert not g.valid("https://example.org/")


def test_valid_empty_server():
    assert not g.valid(u("", 443, HEX32))


# ---------- дедупликация (продакшн-код fetch_all, сеть подменяется) ----------

def _fetch_text(text):
    """Дедуп через реальный fetch_all с подменой urlopen: тестируем продакшн-код."""
    from unittest.mock import patch

    class R:
        def read(self, limit=-1):
            return text.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch.object(g.rq, "urlopen", lambda url, timeout: R()):
        return g.fetch_all({"test": "http://fake/"})[:2]  # (proxies, bad)


def test_dedup_same_proxy_different_server_spelling():
    # один прокси с точкой в конце server и без: это дубликат
    a, b = u("host.name.", 443, HEX32), u("host.name", 443, HEX32)
    out, _ = _fetch_text(a + "\n" + b)
    assert len(out) == 1


def test_dedup_keeps_distinct_proxies():
    a = u("h1", 443, HEX32)
    b = u("h2", 443, HEX32)
    c = u("h1", 8443, HEX32)
    out, _ = _fetch_text("\n".join([a, b, c]))
    assert len(out) == 3


def test_bad_lines_counted():
    text = u("h1", 443, HEX32) + "\ngarbage\n" + u("h1", 443, HEX32)
    out, bad = _fetch_text(text)
    assert len(out) == 1 and bad == 1  # garbage; дубликат не в счёт bad, он отфильтрован как seen


def test_dedup_case_insensitive(monkeypatch):
    """Хост и hex-секрет регистронезависимы: A.example vs a.example - один прокси."""
    from test_tgproxy_fetch import FakeResponse
    url1 = "https://t.me/proxy?server=A.example&port=443&secret=" + "A" * 32
    url2 = "https://t.me/proxy?server=a.example&port=443&secret=" + "a" * 32
    monkeypatch.setattr(g.rq, "urlopen", lambda req, timeout: FakeResponse(url1 + chr(10) + url2))
    proxies, bad, fails, dups = g.fetch_all({"src": "http://x/"})
    assert len(proxies) == 1
    assert dups == 1
