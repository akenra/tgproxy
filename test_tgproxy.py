"""Спека-тесты для tgproxy_gui: чистые функции parse/valid/fetch/dedup.

Сеть не трогаем: fetch покрыт через parse+valid на живых строках-примерах.
Запуск: python -m pytest test_tgproxy.py -q
"""
import base64
import urllib.parse as ps

import tgproxy_gui as g


def u(server, port, secret):
    return f"https://t.me/proxy?server={server}&port={port}&secret={secret}"


HEX32 = "a" * 32
B64 = "eeNEgYdJvXrFGRMCIMJdCQ"  # реальный пример из живого списка: 16 байт после декода
B64DD = "dd10400103324995"  # hex-строка из живого списка: TDLib читает её как hex, 8 байт


def b64url(data):
    """base64url-написание секрета, как в ссылках t.me."""
    return base64.urlsafe_b64encode(data).decode()


# 6 реальных форм отказа из замера живого списка: TDLib такие секреты не принимает
BAD_SECRETS = [
    "eefb47a1320fa86adfa6e63b425154ce1b",  # 34 hex: ee без домена (17 байт)
    "7nnnAQIAAQAH8AMDhuJMOt0=",  # base64: те же 17 байт ee (в ссылке = уходит как %3D)
    "YyEEgYdJXfrFmgMCIMJdjjRueWVrdGFuZXQA",  # base64url: 27 байт, первый байт 0x63
    b64url(bytes([0x6a]) + bytes(21)),  # 22 байта, первый байт 0x6a
    b64url(bytes([0x10]) + b"x" * 180),  # 181 байт, первый байт 0x10
    "A" * 22,  # hex чётной длины: 11 байт, а не 16 после base64-декода
]


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


# ---------- tdlib_secret_ok: зеркало TDLib ProxySecret ----------

def test_tdlib_secret_ok_accepts_tdlib_forms():
    """plain16, 16 байт с dd/ee (правило 16 байт сильнее префикса), dd17,
    ee+домен в hex и base64url, граница ровно 199 байт."""
    assert g.tdlib_secret_ok(HEX32)
    assert g.tdlib_secret_ok("AA" * 16)  # регистр hex не важен
    assert g.tdlib_secret_ok("dd" + "c" * 30)
    assert g.tdlib_secret_ok("ee" + "c" * 30)
    assert g.tdlib_secret_ok("dd" + "c" * 32)  # 17 байт: dd-форма
    assert g.tdlib_secret_ok("ee" + "ab" * 90)  # ee + домен, hex
    assert g.tdlib_secret_ok(b64url(bytes([0xee]) + bytes(16) + b"example.com"))
    assert g.tdlib_secret_ok(b64url(bytes([0xee]) + bytes(198)))  # 199 байт: граница


def test_tdlib_secret_ok_rejects_live_bad_forms():
    """6 форм, отвергнутых на живом списке: TDLib такие секреты не принимает."""
    for secret in BAD_SECRETS:
        assert not g.tdlib_secret_ok(secret), secret


def test_tdlib_secret_ok_rejects_other_shapes():
    assert not g.tdlib_secret_ok("")  # пусто
    assert not g.tdlib_secret_ok("####")  # не декодируется ни одним способом
    assert not g.tdlib_secret_ok("a" * 30)  # 15 байт: короче 16
    assert not g.tdlib_secret_ok("ee" + "00" * 199)  # 200 байт: длиннее 199
    assert not g.tdlib_secret_ok(b64url(bytes([0xee]) + bytes(199)))  # то же в base64url


# ---------- valid: секрет ----------

def test_valid_hex32():
    assert g.valid(u("x", 443, HEX32))


def test_valid_prefixed_hex_forms():
    """Живые формы: 16 байт с dd/ee в первом байте, dd17, ee+домен."""
    assert g.valid(u("x", 443, "dd" + "c" * 30))
    assert g.valid(u("x", 443, "ee" + "c" * 30))
    assert g.valid(u("x", 443, "dd" + "c" * 32))
    assert g.valid(u("x", 443, "ee" + "ab" * 90))


def test_valid_hex_wrong_length():
    # нечётная длина из hex-символов это битый hex, не base64:
    # как в TDLib, декодируем base64-ветками исходную строку
    assert not g.valid(u("x", 443, "a" * 31))
    assert not g.valid(u("x", 443, "a" * 33))


def test_valid_base64url_live_sample():
    # реальная строка из публичного списка: base64url, 16 байт после декода
    assert g.valid(u("good.cipservice.co.uk", 2083, B64))


def test_valid_rejects_tdlib_incompatible_secret():
    """Регресс: мягкое правило пропускало эти секреты в буфер, а TDLib их отвергает."""
    assert not g.valid(u("x", 443, "A" * 22))  # hex чётной длины: 11 байт, не base64
    assert not g.valid(u("x", 443, "dd" + "b" * 62))  # 32 байта с dd: dd только ровно на 17
    assert not g.valid(u("x", 443, "z" * 32))  # base64url: 24 байта, первый байт 0xcf
    assert not g.valid(u("x", 443, BAD_SECRETS[0]))
    assert not g.valid(u("x", 443, BAD_SECRETS[1].replace("=", "%3D")))  # = в ссылке как %3D
    assert not g.valid(u("bookmook.hichibehichi-co.info.", 2882, B64DD))


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


# ---------- проверка обновлений: сравнение версий и классификация ----------

def test_is_newer_numeric_triples():
    """Сравнение числовыми тройками: новее, равно, старее; 10.0.0 новее 9.9.9."""
    assert g.is_newer("v1.0.6", "1.0.5")
    assert g.is_newer("v10.0.0", "9.9.9")
    assert not g.is_newer("v1.0.5", "1.0.5")
    assert not g.is_newer("v1.0.4", "1.0.5")
    assert g.is_newer("V2.0.0", "1.0.5")  # регистр "v" не важен


def test_is_newer_junk_is_never_newer():
    """Суффиксы, мусор и не-строки не роняют сравнение: трактуются как «не новее»."""
    for tag in ("", "мусор", "v1.0.6-beta", "1.0", "v1.0.5.1", "release-2", None, 5):
        assert g.is_newer(tag, "1.0.5") is False, tag
    assert g.is_newer("v9.9.9", "мусор") is False  # нераспознанная текущая тоже


def test_classify_release_payloads():
    """Тело ответа GitHub: мусорный tag_name - это «последняя версия», не ошибка."""
    assert g.classify_release({"tag_name": "v9.9.9"}) == ("newer", "v9.9.9")
    assert g.classify_release({"tag_name": "v" + g.APP_VERSION}) == ("current", None)
    assert g.classify_release({"tag_name": "v0.0.1"}) == ("current", None)
    assert g.classify_release({"tag_name": "v1.0.6-rc1"}) == ("current", None)
    assert g.classify_release({}) == ("current", None)
    assert g.classify_release(None) == ("current", None)
    assert g.classify_release("не словарь") == ("current", None)
