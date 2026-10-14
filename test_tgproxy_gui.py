"""Headless-тесты GUI-логики tgproxy_gui: состояние App, потоки, busy-защита, enable().

Tk работает в виртуальном display (Xvfb) или штатно на Windows; сеть не трогаем:
alive() подменяется заглушкой через monkeypatch, потоки - реальные (ThreadPoolExecutor).

Запуск: python -m pytest test_tgproxy_gui.py -q
Пропуск без дисплея: тесты сами скипаются, если Tk не стартует (CI без X).
"""
import json
import threading
import sys
import time

import pytest

import tgproxy_gui as g



@pytest.fixture(scope="module")
def tk():
    try:
        from tkinter import Tk
        root = Tk()
    except Exception as e:  # noqa: BLE001 - CI без дисплея
        pytest.skip(f"Tk недоступен: {e}")
    root.withdraw()  # окно не показываем: headless по смыслу
    yield root
    root.destroy()


@pytest.fixture(autouse=True)
def ru_default_lang(monkeypatch):
    """Язык в тестах по умолчанию русский, независимо от локали машины:
    автоопределение не должно зависеть от окружения CI."""
    monkeypatch.setattr(g.locale, "getlocale", lambda *a: ("ru_RU", "cp1251"))
    g.set_lang("ru")


@pytest.fixture()
def app(tk, tmp_path, monkeypatch):
    """App с SETTINGS на временный файл: кэш-тесты не мусорят друг другу.
    monkeypatch сам восстановит значение после теста.
    Замечание: _on_close в тесте destroy'ит фикстурный root - после такого теста
    соседние тесты модуля не живут; держим destroy-тесты на отдельных root."""
    import tgproxy_gui as _g
    monkeypatch.setattr(_g, "SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(_g.locale, "getlocale", lambda *a: ("ru_RU", "cp1251"))
    _g.set_lang("ru")  # тесты ждут русский интерфейс независимо от локали машины
    a = _g.App(tk)
    yield a
    a._closing = True  # рвём _drain-цикл: не копим таймеры на общий module-scoped root


def u(server, port, secret="a" * 32):
    return f"https://t.me/proxy?server={server}&port={port}&secret={secret}"


def tmp_settings(app):
    """Путь к временному SETTINGS из fixture app (для записи кэша до действия)."""
    import tgproxy_gui as _g
    return __import__("pathlib").Path(_g.SETTINGS)


def pump(root, ms=250):
    """Прокрутить событийный цикл Tk ms миллисекунд без mainloop."""
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def wait_done(app, tk, limit=500):
    """Крутить цикл, пока фоновая работа не закончится (busy снимется)."""
    for _ in range(limit):
        pump(tk, 20)
        if not app.busy:
            return
    raise AssertionError("фоновая работа не завершилась: busy завис")


def click_button(win, text):
    """Нажать кнопку с подписью text в окне win."""
    for child in win.children.values():
        try:
            label = child.cget("text")
        except Exception:  # noqa: BLE001 - у Entry/Frame опции text нет
            continue
        if label == text:
            child.invoke()
            return
    raise AssertionError(f"кнопка {text!r} не найдена")


# ---------- состояние и busy-защита ----------

def test_initial_state(app):
    assert app.proxies == []
    assert app.busy is False
    assert "1. Проверить прокси" in app.label.cget("text")


def test_busy_blocks_buttons(app, tk, monkeypatch):
    monkeypatch.setattr(g, "alive", lambda url: True)  # не ходить в сеть из теста
    app.proxies = [(u("h", 443), "h", 443)]
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    app.status = {app.proxies[0][0]: None}
    app._busy_on()
    assert app.busy is True
    assert str(app.btn_load.cget("state")) == "disabled"
    assert str(app.btn_enable.cget("state")) == "disabled"
    # повторные load/check/enable игнорируются
    app.load_and_check(); app.load_and_check(); app.enable()
    app._busy_off()
    assert app.busy is False
    assert str(app.btn_enable.cget("state")) == "normal"


def test_check_progress_not_animated(app, tk, monkeypatch):
    """Регресс: check() раньше включал indeterminate-анимацию поверх determinate,
    полоса скакала 0-100. В determinate-режиме анимация start() не должна идти."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: None}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    assert str(app.progress.cget("mode")) == "determinate"
    assert app.progress.cget("maximum") == 1
    for _ in range(500):
        pump(tk, 20)
        if not app.busy:
            break
    assert app.busy is False
    assert float(app.progress.cget("value")) == 1.0
    pump(tk, 50)  # даём do-анимации тик, если она ошибочно запущена
    assert str(app.progress.cget("mode")) == "determinate"


def test_check_without_proxies_hints(app, tk, tmp_path, monkeypatch):
    """Пустое состояние: кнопка качает списки, а не ругается."""
    monkeypatch.setattr(g, "SETTINGS", str(tmp_path / "s.json"))
    monkeypatch.setattr(g, "fetch_all", lambda urls: ([(u("h", 443), "h", 443)], 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    assert app.busy is False
    assert app.status[u("h", 443)] is True


def test_enable_without_proxies_hints(app):
    app.enable()
    assert "Проверить прокси" in app.label.cget("text")


def test_enable_without_alive_hints(app):
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: False}
    app.enable()
    assert "Проверить" in app.label.cget("text")


# ---------- потоки: _check с подменой alive ----------

def test_check_updates_status_and_unblocks(app, tk, monkeypatch):
    n = 5
    app.proxies = [(u(f"h{i}", 443), f"h{i}", 443) for i in range(n)]
    app.status = {p[0]: None for p in app.proxies}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    # первый живой, остальные мёртвые; вместо сети - мгновенная заглушка
    monkeypatch.setattr(g, "alive", lambda url: "server=h0&" in url)
    app.load_and_check()
    # крутим цикл, пока поток не закончит (busy снимется)
    for _ in range(500):
        pump(tk, 20)
        if not app.busy:
            break
    assert app.busy is False, "поток не завершился: busy завис"
    assert all(v is not None for v in app.status.values()), "не все прокси проверены"
    assert sum(app.status.values()) == 1
    pump(tk, 200)  # refresh батчится на 100мс: дожидаемся финального текста
    assert "Готово: живых 1 из 5" in app.label.cget("text")


def test_check_alive_exception_still_unblocks(app, tk, monkeypatch):
    """Исключение в alive() не должно навсегда выключать кнопки."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: None}
    app._fetched_url = app.custom_url or ""  # список скачан ранее

    def boom(_):
        raise RuntimeError("сеть умерла")

    monkeypatch.setattr(g, "alive", boom)
    app.load_and_check()
    for _ in range(500):
        pump(tk, 20)
        if not app.busy:
            break
    assert app.busy is False, "исключение в alive() заблокировало кнопки навсегда"


# ---------- refresh: сортировка и подписи ----------

def test_refresh_hides_dead_shows_alive_and_unknown(app):
    """Мёртвые не отображаются (визуальный шум), живые сверху, непроверенные следом."""
    urls = [u("dead", 443), u("alive", 443), u("unknown", 443)]
    app.proxies = [(x, x.split("server=")[1], 443) for x in urls]
    app.status = {urls[0]: False, urls[1]: True, urls[2]: None}
    app.refresh("тест")
    app._do_refresh()  # сразу, не дожидаясь батчинга
    rows = app.listbox.get(0, "end")
    assert len(rows) == 2, rows  # мёртвого нет
    assert rows[0].startswith("[живой]"), rows
    assert rows[1].startswith("[ ?"), rows
    assert app.row_urls == [urls[1], urls[2]]  # двойной клик откроет именно живого


def test_refresh_resurrected_dead_reappears(app):
    """Мёртвый, воскресший при перепроверке (False -> True), снова виден."""
    url = u("back", 443)
    app.proxies = [(url, "back", 443)]
    app.status = {url: True}  # был мёртв, перепроверка сказала жив
    app._do_refresh()
    rows = app.listbox.get(0, "end")
    assert len(rows) == 1 and rows[0].startswith("[живой]")


def test_double_click_disabled_when_busy(app, tk, monkeypatch):
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: True}
    app._do_refresh()
    opened = []
    monkeypatch.setattr(g.webbrowser, "open", lambda x: opened.append(x) or True)
    app._busy_on()
    app.open_selected(None)
    assert opened == [], "клик при busy открыл ссылку"
    app._busy_off()
    app.listbox.selection_set(0)  # как реальный клик по первой строке
    app.open_selected(None)
    assert len(opened) == 1
    assert opened[0].startswith("tg://proxy?")


# ---------- enable: буфер и tg:// ----------

def test_enable_puts_alive_in_clipboard_and_opens_tg(app, tk, monkeypatch):
    urls = [u("a", 443), u("b", 443), u("c", 443)]
    app.proxies = [(x, x.split("server=")[1], 443) for x in urls]
    app.status = {urls[0]: True, urls[1]: False, urls[2]: True}
    opened = []
    monkeypatch.setattr(g.webbrowser, "open", lambda x: opened.append(x) or True)
    app.enable()
    pump(tk, 50)
    clip = tk.clipboard_get()
    assert urls[0] in clip and urls[2] in clip
    assert urls[1] not in clip  # мёртвый не попал
    assert len(opened) == 1
    assert opened[0] == "tg://proxy?" + urls[0].split("?", 1)[1]  # первый живой, без сортировки


def test_enable_webbrowser_exception_falls_back(app, tk, monkeypatch):
    """webbrowser.open бросает исключение вместо False."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: True}

    def boom(_):
        raise RuntimeError("нет браузера")

    monkeypatch.setattr(g.webbrowser, "open", boom)
    app.enable()  # не должно упасть
    pump(tk, 50)
    assert "буфере" in app.label.cget("text") or "Не удалось" in app.label.cget("text")


# ---------- settings ----------

def test_load_settings_corrupted_json(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text("{битый json", encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))
    assert g.load_settings() == {}


def test_load_settings_wrong_types(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text('["массив", "вместо", "dict"]', encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))
    assert g.load_settings() == {}


def test_load_settings_ok(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text('{"list_url": "https://example.com/list.txt"}', encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))
    assert g.load_settings()["list_url"] == "https://example.com/list.txt"


# ---------- load: фоновая загрузка с подменой fetch_all ----------

def test_load_and_check_full_flow(app, tk, monkeypatch):
    """Кнопка 1: fetch + сразу проверка; статусы заполняются, финальный текст с итогом."""
    fresh = [(u("live", 443), "live", 443), (u("dead", 443), "dead", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: "server=live&" in url)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert app.status == {u("live", 443): True, u("dead", 443): False}
    text = app.label.cget("text")
    assert "Готово: живых 1 из 2" in text
    assert "2. Включить" in text
    assert app.busy is False


def test_load_and_check_merges_cached_alive(app, tk, monkeypatch):
    """Свежий список дополняется прошлыми живыми из кэша: их тоже перепроверяем."""
    cached_url = u("cached", 443)
    p = tmp_settings(app)
    p.write_text(json.dumps({"last_results": {cached_url: True}}), encoding="utf-8")
    fresh = [(u("new", 443), "new", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    assert {pr[0] for pr in app.proxies} == {cached_url, u("new", 443)}
    assert all(app.status.values())
    pump(tk, 200)
    assert "Готово: живых 2 из 2" in app.label.cget("text")


def test_load_and_check_cached_dead_not_merged(app, tk, monkeypatch):
    """Мёртвые из кэша не добавляются к свежему списку: они своё отживали."""
    p = tmp_settings(app)
    p.write_text(json.dumps({"last_results": {u("dead", 443): False}}), encoding="utf-8")
    fresh = [(u("new", 443), "new", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    assert [pr[0] for pr in app.proxies] == [u("new", 443)]


def test_load_and_check_custom_url_only(app, tk, monkeypatch):
    """Свой URL в настройках: fetch_all получает только его, не PRESETS."""
    got = {}

    def fake_fetch(urls):
        got["urls"] = dict(urls)
        return [(u("h", 443), "h", 443)], 0, [], 0

    app.custom_url = "https://example.com/list.txt"
    monkeypatch.setattr(g, "fetch_all", fake_fetch)
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    assert got["urls"] == {"свой": "https://example.com/list.txt"}


def test_load_and_check_empty_result_message(app, tk, monkeypatch):
    monkeypatch.setattr(g, "fetch_all", lambda urls: ([], 3, [("zakky8", "err")], 0))
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert "пусты" in app.label.cget("text")
    assert app.busy is False


def test_load_and_check_network_error(app, tk, monkeypatch):
    def boom(urls):
        raise RuntimeError("нет сети")

    monkeypatch.setattr(g, "fetch_all", boom)
    app.load_and_check()
    wait_done(app, tk)
    assert "Ошибка загрузки" in app.label.cget("text")
    assert app.busy is False


def test_load_and_check_counts_in_final_message(app, tk, monkeypatch):
    """Счётчики загрузки (битые, упавшие источники, дубликаты) - в финальном тексте."""
    monkeypatch.setattr(
        g, "fetch_all",
        lambda urls: ([(u("h", 443), "h", 443)], 7, [("soli", "HTTP 500")], 2))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    text = app.label.cget("text")
    assert "некорректных строк: 7" in text
    assert "soli: HTTP 500" in text
    assert "дубликатов отброшено: 2" in text
    assert "Готово: живых 1 из 1" in text


# ---------- _drain: очередь колбэков ----------

def test_drain_executes_queued_callbacks(app, tk):
    calls = []
    app._after(0, calls.append, "x")
    pump(tk, 100)
    assert calls == ["x"]


def test_drain_survives_dead_window(app, tk, tmp_path, monkeypatch):
    """after() на уничтоженном виджете не роняет _drain."""
    monkeypatch.setattr(g, "ERROR_LOG", str(tmp_path / "err.log"))  # ошибка колбэка - в tmp-лог
    from tkinter import Toplevel
    w = Toplevel(tk)
    w.destroy()
    app._after(0, w.lift)
    pump(tk, 100)  # не должно упасть


# ---------- двойной клик: fallback и пустое выделение ----------

def test_double_click_webbrowser_false_falls_back(app, tk, monkeypatch):
    """webbrowser.open вернул False (нет обработчика tg://): статус с подсказкой и ссылкой."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: True}
    app._do_refresh()
    monkeypatch.setattr(g.webbrowser, "open", lambda x: False)
    app.listbox.selection_set(0)
    app.open_selected(None)
    assert "Не удалось" in app.label.cget("text")
    assert app.row_urls[0] in app.label.cget("text")  # ссылка отдаётся текстом


def test_double_click_no_selection_is_noop(app, tk, monkeypatch):
    opened = []
    monkeypatch.setattr(g.webbrowser, "open", lambda x: opened.append(x) or True)
    app.open_selected(None)  # пустой список, нет выделения
    assert opened == []


# ---------- справка и настройки: окна ----------

def test_show_help_creates_and_reuses_window(app):
    app.show_help()
    assert app._help_win is not None and app._help_win.winfo_exists()
    first = app._help_win
    app.show_help()
    assert app._help_win is first  # повторный вызов не плодит окон
    first.destroy()
    app.show_help()  # после закрытия создаётся заново
    assert app._help_win.winfo_exists()
    app._help_win.destroy()


def test_show_settings_creates_window(app):
    app.show_settings()
    assert app._settings_win is not None and app._settings_win.winfo_exists()
    app._settings_win.destroy()


def test_settings_save_custom_url(app, tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    monkeypatch.setattr(g, "SETTINGS", str(p))
    app.custom_url = None
    app.show_settings()
    entry = app._settings_win.children["!entry"]
    entry.delete(0, "end")
    entry.insert(0, "https://example.com/list.txt")
    click_button(app._settings_win, "Сохранить")
    assert app.custom_url == "https://example.com/list.txt"
    assert p.exists() and "example.com" in p.read_text(encoding="utf-8")


def test_settings_save_rejects_non_http(app, tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    monkeypatch.setattr(g, "SETTINGS", str(p))
    app.custom_url = None
    app.show_settings()
    entry = app._settings_win.children["!entry"]
    entry.delete(0, "end")
    entry.insert(0, "ftp://example.com")
    click_button(app._settings_win, "Сохранить")
    assert app.custom_url is None  # не сохранилось
    assert not p.exists()
    assert "http" in app.label.cget("text")


def test_settings_preset_button_clears_entry(app):
    app.show_settings()
    entry = app._settings_win.children["!entry"]
    entry.insert(0, "https://example.com/x")
    click_button(app._settings_win, "Все предустановленные")
    assert entry.get() == ""
    app._settings_win.destroy()


def test_settings_reopen_reuses_window(app):
    app.show_settings()
    first = app._settings_win
    app.show_settings()
    assert app._settings_win is first  # повторный вызов не плодит окон
    first.destroy()


def test_settings_save_oserror_hint(app, tmp_path, monkeypatch):
    """Регресс: нельзя писать настройки в недоступное место - не падать, а подсказать."""
    import tgproxy_gui as gmod
    # SETTINGS указывает на каталог: open() бросит PermissionError (подкласс OSError)
    monkeypatch.setattr(gmod, "SETTINGS", str(tmp_path))
    app.custom_url = None
    app.show_settings()
    entry = app._settings_win.children["!entry"]
    entry.delete(0, "end")
    entry.insert(0, "https://example.com/list.txt")
    click_button(app._settings_win, "Сохранить")
    assert "Не удалось сохранить" in app.label.cget("text")
    assert app.custom_url is None  # значение не принято
    app._settings_win.destroy()


def test_double_click_webbrowser_exception_falls_back(app, tk, monkeypatch):
    """webbrowser.open бросил исключение при двойном клике: статус с подсказкой."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: True}
    app._do_refresh()

    def boom(_):
        raise RuntimeError("нет браузера")

    monkeypatch.setattr(g.webbrowser, "open", boom)
    app.listbox.selection_set(0)
    app.open_selected(None)  # не должно упасть
    assert "Не удалось" in app.label.cget("text")


def test_drain_swallows_after_error_on_destroyed_root(app, tk, monkeypatch, tmp_path):
    """root.after() бросил (окно закрывается): _drain глотает и продолжает поллить."""
    monkeypatch.setattr(g, "ERROR_LOG", str(tmp_path / "err.log"))  # не писать в реальный лог
    done = []

    # вариант 1: колбэк пользователя "умер" вместе с окном
    def broken_cb():
        raise RuntimeError("main thread is not in main loop")

    app._after(0, broken_cb)
    app._after(0, lambda: done.append("x"))
    pump(tk, 150)
    assert done == ["x"]  # очередь додренировалась после исключения

    # вариант 2: сам root.after() бросил на колбэке - reschedule в finally спасает
    orig_after = app.root.after
    calls = []

    def flaky_after(ms, fn, *a):
        if getattr(fn, "__func__", fn) is not app.__class__._drain:
            raise RuntimeError("main thread is not in main loop")
        return orig_after(ms, fn, *a)

    app.root.after = flaky_after
    app._after(0, calls.append, "y")
    pump(tk, 100)  # тик _drain при сломанном after(): 'y' проглатывается веткой 205-206
    app.root.after = orig_after  # чиним: следующий тик _drain додренирует
    app._after(0, calls.append, "z")
    pump(tk, 200)
    assert calls == ["z"]  # 'y' потерян при сломанном after (задокументированное поведение)


def test_on_close_stops_drain(app, tk):
    """WM_DELETE_WINDOW: _closing=True, _drain больше не перезапускается."""
    app._closing = True  # без реального destroy: root жив для остальных тестов модуля
    calls = []
    app._after(0, calls.append, "x")  # кладётся в очередь, но _drain её не дренирует
    pump(tk, 100)
    assert calls == [] and app._closing is True
    app._closing = False
    app.root.after(25, app._drain)  # в жизни: новый App или перезапуск; тут рестарт тика
    pump(tk, 150)
    assert calls == ["x"]


def test_on_close_destroy_stops_loop(app, tk):
    """Полный путь _on_close: destroy сжигает root, _on_close ставит _closing."""
    # отдельный root, чтобы не убить общий fixture-root
    from tkinter import Tk
    r2 = Tk()
    r2.withdraw()
    app2 = g.App(r2)
    r2.protocol("WM_DELETE_WINDOW", app2._on_close)
    app2._on_close()  # destroy внутри
    assert app2._closing is True
    try:
        r2.update()  # уничтоженный root: after-цикл мёртв, исключений быть не должно
    except Exception:
        pass  # TclError на уничтоженном root - ожидаемо, финальное состояние проверено


def test_tk_error_writes_log(app, tmp_path, monkeypatch):
    """report_callback_exception пишет traceback в лог-файл рядом с программой."""
    log = tmp_path / "err.log"
    monkeypatch.setattr(g, "ERROR_LOG", str(log))
    try:
        raise RuntimeError("тестовое исключение колбэка")
    except RuntimeError:
        app._tk_error(*sys.exc_info())
    assert log.exists() and "тестовое исключение колбэка" in log.read_text(encoding="utf-8")


def test_after_from_worker_thread(app, tk):
    """Ядро потокобезопасности: _after из не-main потока доходит до main через очередь."""
    import threading
    calls = []
    t = threading.Thread(target=lambda: app._after(0, calls.append, "from-worker"))
    t.start()
    t.join()
    assert calls == []  # очередь, ещё не исполнено
    pump(tk, 100)
    assert calls == ["from-worker"]


def test_refresh_batches_multiple_calls(app, tk):
    """5 refresh подряд -> один _do_refresh, в списке последний текст."""
    for i in range(5):
        app.refresh(f"текст {i}")
    pump(tk, 250)
    assert app.label.cget("text") == "текст 4"


def test_check_rerun_overwrites_old_status(app, tk, monkeypatch):
    """Повторное «Проверить» после завершения: статусы перезаписываются, busy снимается."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: True}  # старый прогон
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", lambda url: False)
    app.load_and_check()
    wait_done(app, tk)
    assert app.status[app.proxies[0][0]] is False
    pump(tk, 200)  # refresh батчится на 100мс: дожидаемся финального текста
    assert "Готово: живых 0 из 1" in app.label.cget("text")


def test_settings_save_whitespace_and_uppercase(app, tmp_path, monkeypatch):
    """URL с пробелами обрезается; HTTPS:// в верхнем регистре принимается."""
    p = tmp_path / "s.json"
    monkeypatch.setattr(g, "SETTINGS", str(p))
    app.custom_url = None
    app.show_settings()
    entry = app._settings_win.children["!entry"]
    entry.delete(0, "end")
    entry.insert(0, "  HTTPS://example.com/list.txt  ")
    click_button(app._settings_win, "Сохранить")
    assert app.custom_url == "HTTPS://example.com/list.txt"
    app._settings_win.destroy()


def test_load_settings_non_str_value(tmp_path, monkeypatch):
    """dict есть, но list_url не строка (число): настройки отбрасываются."""
    p = tmp_path / "s.json"
    p.write_text('{"list_url": 123}', encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))
    assert g.load_settings() == {}


# ---------- selftest как часть пакета ----------

def test_selftest_smoke():
    g.selftest()  # встроенные assert'ы не должны расходиться с pytest-пакетом


# ---------- флаги командной строки: --version и --self-test ----------

def test_main_version_prints_and_returns_0(capsys):
    """--version печатает 'tgproxy <версия>' и возвращает 0."""
    assert g.main(["--version"]) == 0
    assert capsys.readouterr().out == "tgproxy %s\n" % g.APP_VERSION


def test_main_reads_sys_argv_when_none(capsys, monkeypatch):
    """argv=None (реальный запуск): аргументы берутся из sys.argv."""
    monkeypatch.setattr(sys, "argv", ["tgproxy_gui.py", "--version"])
    assert g.main() == 0
    assert capsys.readouterr().out == "tgproxy %s\n" % g.APP_VERSION


def test_main_version_without_stdout_is_safe(monkeypatch):
    """windowed-сборка: sys.stdout is None - --version не падает и даёт 0."""
    monkeypatch.setattr(g.sys, "stdout", None)
    assert g.main(["--version"]) == 0


def test_main_self_test_ok():
    """--self-test: самопроверка проходит, контракт - код возврата 0."""
    assert g.main(["--self-test"]) == 0


def test_main_self_test_failure_returns_1(monkeypatch):
    """Сбой проверки (подменённая valid() бросает) - код 1, без исключения наружу."""
    def boom(line):
        raise RuntimeError("подменённая проверка")
    monkeypatch.setattr(g, "valid", boom)
    assert g.main(["--self-test"]) == 1


def test_main_self_test_creates_no_tk_no_network(monkeypatch):
    """Самопроверка строго без окна Tk и без сети: попытка - сразу сбой."""
    def bomb(*a, **kw):
        raise AssertionError("самопроверка не должна трогать Tk и сеть")
    monkeypatch.setattr(g, "Tk", bomb)
    monkeypatch.setattr(g.socket, "create_connection", bomb)
    monkeypatch.setattr(g.rq, "urlopen", bomb)
    assert g.main(["--self-test"]) == 0


def test_main_unknown_args_fall_back_to_gui(monkeypatch):
    """Незнакомые аргументы - обычный путь GUI (поведение не меняется)."""
    calls = []

    class Root:
        def mainloop(self):
            calls.append("mainloop")

    monkeypatch.setattr(g, "selftest", lambda: calls.append("selftest"))
    monkeypatch.setattr(g, "Tk", lambda: calls.append("Tk") or Root())
    monkeypatch.setattr(g, "App", lambda root: calls.append("App"))
    monkeypatch.setattr(g.os, "_exit", lambda code: calls.append(("exit", code)))
    g.main(["--bogus"])
    assert calls == ["selftest", "Tk", "App", "mainloop", ("exit", 0)]


def test_save_last_results_oserror_swallowed(monkeypatch):
    """Ошибка записи кэша результатов не роняет проверку (Tg-proxy кэш не критичен)."""
    def boom(path, *a, **kw):
        raise OSError("диск переполнен")

    monkeypatch.setattr("builtins.open", boom)
    g.save_last_results({"x": True})  # не должно бросить


def new_root():
    """Отдельный Tk для тестов, где фикстурный root занят."""
    try:
        from tkinter import Tk
        r = Tk()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Tk недоступен: {e}")
    r.withdraw()
    return r


# ---------- старт из кэша прошлых результатов ----------

def test_startup_loads_cached_proxies(app, tk, tmp_path, monkeypatch):
    """При старте кэш last_results превращается в список со статусами, без сети."""
    p = tmp_path / "s.json"
    url1, url2 = u("live.example", 443), u("dead.example", 443)
    p.write_text(json.dumps({"last_results": {url1: True, url2: False}}), encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))

    app2 = g.App(new_root())
    try:
        assert [pr[0] for pr in app2.proxies] == [url1, url2]
        assert app2.status == {url1: True, url2: False}
        pump(tk, 150)  # refresh батчится на 100мс
        assert "кэша" in app2.label.cget("text")
        assert "живых 1" in app2.label.cget("text")
    finally:
        app2._on_close()


def test_startup_skips_invalid_cached(app, tk, tmp_path, monkeypatch):
    """Мусорные ссылки в кэше не попадают в список (валидатор)."""
    p = tmp_path / "s.json"
    good = u("ok.example", 443)
    p.write_text(json.dumps({"last_results": {good: True, "https://t.me/garbage": True,
                                             "https://t.me/proxy?server=x&port=1&secret=short": False}}),
                 encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))

    app2 = g.App(new_root())
    try:
        assert [pr[0] for pr in app2.proxies] == [good]
    finally:
        app2._on_close()


def test_startup_no_cache_empty_list(app, tk, tmp_path, monkeypatch):
    """Нет кэша - список пуст, стартовое сообщение как раньше."""
    p = tmp_path / "s.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))

    app2 = g.App(new_root())
    try:
        assert app2.proxies == []
        assert "кэша" not in app2.label.cget("text")
    finally:
        app2._on_close()


def test_check_zero_alive_suggests_refresh(app, tk, monkeypatch, tmp_path):
    """Проверка с 0 живых предлагает «1. Загрузить» - возможно, список устарел."""
    monkeypatch.setattr(g, "SETTINGS", str(tmp_path / "s.json"))  # не писать кэш в реальный файл
    app.proxies = [(u("x", 443), "x", 443)]
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", lambda url: False)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    text = app.label.cget("text")
    assert "Готово: живых 0" in text
    assert "источники устарели" in text



def test_rerun_clears_stale_load_prefix(app, tk, monkeypatch):
    """Перепроверка без скачивания не показывает старый текст «Загружено N прокси»."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {u("h", 443): True}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    app._load_prefix = "Загружено 999 прокси [древняя ошибка]"  # остался от прошлого прогона
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert "Загружено 999" not in app.label.cget("text")


def test_rerun_branch_no_fetch(app, tk, monkeypatch):
    """Ветка перепроверки: список уже есть - fetch_all не зовётся вовсе."""
    called = []

    def spy_fetch(urls):
        called.append(urls)
        return [], 0, [], 0

    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {u("h", 443): None}
    app._fetched_url = app.custom_url or ""  # список скачан ранее: сейчас перепроверка
    monkeypatch.setattr(g, "fetch_all", spy_fetch)
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    assert called == []  # скачивания не было
    assert app.status[u("h", 443)] is True


def test_settings_save_keeps_cached_results(app, tk, monkeypatch):
    """Запись настроек не затирает кэш результатов (single writer через lock)."""
    p = tmp_settings(app)
    g.save_last_results({u("h", 443): True})
    # пользователь сохраняет URL, пока воркер писал кэш: кэш должен выжить
    app.show_settings()
    entry = app._settings_win.children["!entry"]
    entry.delete(0, "end")
    entry.insert(0, "https://example.com/list.txt")
    click_button(app._settings_win, "Сохранить")
    app._settings_win.destroy()
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved.get("list_url") == "https://example.com/list.txt"
    assert saved.get("last_results") == {u("h", 443): True}


def test_fixture_app_does_not_touch_real_settings(app, tmp_path):
    """Во время теста с fixture app g.SETTINGS указывает на tmp_path."""
    import tgproxy_gui as _g
    assert _g.SETTINGS.startswith(str(tmp_path))


def test_startup_message_counts_only_visible(app, tk, monkeypatch):
    """В сообщении старта из кэша считаются только видимые строки списка."""
    p = tmp_settings(app)
    url_live, url_dead = u("live", 443), u("gone", 443)
    p.write_text(json.dumps({"last_results": {url_live: True, url_dead: False}}), encoding="utf-8")
    app2 = g.App(new_root())
    try:
        pump(tk, 150)
        text = app2.label.cget("text")
        assert "в списке 1" in text  # только живой виден
        assert "живых 1" in text
    finally:
        app2._on_close()


def test_settings_save_empty_url_keeps_cache(app, tk, monkeypatch):
    """Ветка s.pop("list_url") при пустом URL: кэш результатов сохраняется."""
    p = tmp_settings(app)
    g.save_last_results({u("h", 443): True})
    app.show_settings()
    click_button(app._settings_win, "Все предустановленные")
    click_button(app._settings_win, "Сохранить")
    app._settings_win.destroy()
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert "list_url" not in saved
    assert saved.get("last_results") == {u("h", 443): True}


def test_full_flow_progress_stops_autoincrement(app, tk, monkeypatch):
    """Indeterminate-анимация остановлена при переходе в determinate,
    иначе таймер ttk борется с value из воркера и портит все последующие прогоны."""
    fresh = [(u(f"h{i}", 443), f"h{i}", 443) for i in range(3)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert str(app.progress.cget("mode")) == "determinate"
    assert float(app.progress.cget("value")) == 3.0  # не скачет от автоинкремента


def test_rerun_status_no_leading_dot(app, tk, monkeypatch):
    """При перепроверке статус не начинается с лишней точки."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {u("h", 443): None}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    assert not app.label.cget("text").startswith(". ")



def test_settings_write_atomic_no_lost_list_url(app, tk, monkeypatch):
    """Прерванная запись не оставляет обрезанный файл; list_url переживает кэш-запись."""
    p = tmp_settings(app)
    p.write_text(json.dumps({"list_url": "https://example.com/l.txt"}), encoding="utf-8")
    g.save_last_results({u("h", 443): True})
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["list_url"] == "https://example.com/l.txt"  # не потерян
    assert saved["last_results"] == {u("h", 443): True}


def test_check_saves_cache_with_list_url_intact(app, tk, monkeypatch):
    """После успешного прогона в settings есть last_results, list_url цел."""
    p = tmp_settings(app)
    p.write_text(json.dumps({"list_url": "https://example.com/l.txt"}), encoding="utf-8")
    url = u("h", 443)
    app.proxies = [(url, "h", 443)]
    app.status = {url: None}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", lambda x: True)
    app.load_and_check()
    wait_done(app, tk)
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["last_results"] == {url: True}
    assert saved["list_url"] == "https://example.com/l.txt"


def test_merge_cache_dedups_same_proxy_different_spelling(app, tk, monkeypatch):
    """Кэш и свежий список с одинаковым прокси в разном написании server - не дублируются."""
    url_fresh = u("h", 443)                       # server=h
    url_cached = u("h.", 443)                     # server=h. - то же самое с точкой
    p = tmp_settings(app)
    p.write_text(json.dumps({"last_results": {url_cached: True}}), encoding="utf-8")
    fresh = [(url_fresh, "h", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    assert [pr[0] for pr in app.proxies] == [url_fresh]  # дубль не добавлен



def test_executor_shutdown_on_close_mid_check(tmp_path, monkeypatch):
    """Закрытие окна посреди проверки не оставляет висящий процесс:
    executor выключается с cancel_futures, невыполненные задачи не гоняются.
    Свой Tk: тест уничтожает окно насовсем, фикстурный root не трогаем."""
    import threading as th
    started = th.Event()
    release = th.Event()
    monkeypatch.setattr(g, "SETTINGS", str(tmp_path / "settings.json"))

    def slow_alive(url):
        started.set()
        release.wait(5)  # держим воркер занятым
        return True

    root = new_root()
    app = g.App(root)
    app.proxies = [(u(f"h{i}", 443), f"h{i}", 443) for i in range(50)]
    app.status = {p[0]: None for p in app.proxies}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", slow_alive)
    app.load_and_check()
    started.wait(2)  # хотя бы одна задача пошла
    app._on_close()
    release.set()
    # executor выключен: очередь задач отменена, невыполненные не гоняются
    assert app._ex._shutdown, "executor не выключен при закрытии окна"


def test_do_refresh_tolerates_status_race(app, tk, monkeypatch):
    """Статус сменился None->False между фильтром и сортировкой - не KeyError.
    Регрессия: sort-ключ {True:0, None:1}[st] падал на False."""
    url = u("h", 443)
    app.proxies = [(url, "h", 443)]
    app.status = {url: True}
    app._pending_text = "тест"
    # эмулируем гонку: каждый второй get после фильтра видит False вместо None/True
    class RacingDict(dict):
        """Эмулирует воркер: после первого get всем статусам присваивается False.
        Старый код делал повторные get (фильтр, потом sort-key, потом mark) и падал
        KeyError на {True:0, None:1}[False]; снапшот-версия делает один get на элемент."""
        def __init__(self, base):
            super().__init__(base)
            self.flipped = False

        def get(self, key, default=None):
            st = super().get(key, default)
            if not self.flipped:
                self.flipped = True
                for k in self:
                    super().__setitem__(k, False)  # "воркер успел" для всех последующих get
            return st

    app.status = RacingDict(app.status)
    app._do_refresh()  # не должно бросить KeyError
    # снапшот взял статусы до флипа: строка осталась, пометка по старому (живому) статусу
    assert app.listbox.size() == 1  # не упало; элемент обработан по snapshot-статусу


def test_executor_created_in_main_thread(app, tk, monkeypatch):
    """executor создаётся до старта потока - закрытие окна в любом
    интервале гарантированно его выключает."""
    fresh = [(u("h", 443), "h", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()  # fetch уходит в поток, _ex ещё нет
    assert getattr(app, "_ex", None) is None or app._ex._shutdown is False
    wait_done(app, tk)
    assert app._ex._shutdown is True  # with-блок закрыл



def test_load_worker_catchall_releases_busy(app, tk, monkeypatch):
    """Неожиданное исключение в хвосте _load_and_check не зависает busy."""
    def boom_fetch(urls):
        return [(u("h", 443), "h", 443)], 0, [], 0

    monkeypatch.setattr(g, "fetch_all", boom_fetch)
    monkeypatch.setattr(g, "valid", lambda url: (_ for _ in ()).throw(ValueError("кэш битый")))
    p = tmp_settings(app)
    p.write_text(json.dumps({"last_results": {u("cached", 443): True}}), encoding="utf-8")
    app.custom_url = None
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert app.busy is False, "busy завис: кнопки не разблокировались"
    assert "прервана" in app.label.cget("text").lower() or "ошибка" in app.label.cget("text").lower()


def test_check_iteration_survives_cancelled_error(app, tk, monkeypatch):
    """CancelledError (BaseException с 3.8) от отменённых фьючерсов гасится,
    выход штатный через finally, threading-excepthook не дёргается."""
    import concurrent.futures as cf
    from concurrent.futures import ThreadPoolExecutor

    real_init = ThreadPoolExecutor.__init__

    def patched_init(self, *a, **kw):
        real_init(self, *a, **kw)
        real_map = self.map

        def map_with_cancel(fn, *iterables):
            def gen():
                yield True  # один результат прошёл
                raise cf.CancelledError()  # дальше фьючерсы отменены закрытием окна
            return gen()

        self.map = map_with_cancel

    monkeypatch.setattr(ThreadPoolExecutor, "__init__", patched_init)

    url = u("h", 443)
    app.proxies = [(url, "h", 443)]
    app.status = {url: None}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", lambda x: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert app.busy is False


def test_cancelled_error_branch_direct(app, tk, monkeypatch):
    """CancelledError из итератора map гасится в главном потоке."""
    import concurrent.futures as cf

    def gen():
        yield True
        raise cf.CancelledError()

    urls = [u(f"h{i}", 443) for i in range(3)]  # zip тянет urls[1] -> next(results) -> CancelledError
    app.proxies = [(u, f"h{i}", 443) for i, u in enumerate(urls)]
    app.status = {u: None for u in urls}
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    monkeypatch.setattr(g, "alive", lambda x: True)
    # вызываем _check в главном потоке с фейковым executor'ом
    class FakeEx:
        def __init__(self):
            self._shutdown = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._shutdown = True
            return False

        def map(self, fn, urls):
            return gen()

    app._ex = FakeEx()
    app._busy_on(indeterminate=False)
    app._check()  # в главном потоке: CancelledError гасится, finally снимает busy
    pump(tk, 200)
    assert app.busy is False


def test_refresh_keeps_scroll_position(app):
    """rebuild списка не сбрасывает скролл посреди проверки."""
    urls = [u(f"h{i}", 443) for i in range(30)]
    app.proxies = [(x, f"h{i}", 443) for i, x in enumerate(urls)]
    app.status = {x: None for x in urls}
    app.refresh("")
    app._do_refresh()
    app.listbox.yview_moveto(0.5)
    app.refresh("")
    app._do_refresh()  # тот же контент: скролл должен выжить
    assert abs(app.listbox.yview()[0] - 0.5) < 0.01


def test_refresh_restores_selection(app):
    """Выделение переживает rebuild (ветка selection_set)."""
    urls = [u(f"h{i}", 443) for i in range(3)]
    app.proxies = [(x, f"h{i}", 443) for i, x in enumerate(urls)]
    app.status = {x: None for x in urls}
    app.refresh("")
    app._do_refresh()
    app.listbox.selection_set(1)
    app.refresh("")
    app._do_refresh()
    assert 1 in app.listbox.curselection()


def test_load_prefix_counts_bad_only(app, tk, monkeypatch):
    """Ветка «только некорректные строки» (без дубликатов)."""
    fresh = [(u("h", 443), "h", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 5, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert "некорректных строк: 5" in app.label.cget("text")
    assert "дубликатов" not in app.label.cget("text")



def test_refresh_pending_not_stuck_when_after_fails(app, tk, monkeypatch, tmp_path):
    """MED: root.after в _drain бросил (окно умирает) - _refresh_pending сбрасывается,
    refresh() не замирает навсегда."""
    monkeypatch.setattr(g, "ERROR_LOG", str(tmp_path / "err.log"))
    real_after = app.root.after
    calls = {"n": 0}

    def failing_after(ms, fn, *a):
        if fn == app._do_refresh:
            calls["n"] += 1
            raise RuntimeError(" Tcl: application has been destroyed")
        return real_after(ms, fn, *a)

    app.root.after = failing_after
    app.refresh("текст 1")            # план _do_refresh уйдёт в очередь
    pump(tk, 150)                      # _drain попытается after() -> RuntimeError -> лог
    assert calls["n"] == 1
    assert app._refresh_pending is False, "флаг залип: refresh замер навсегда"
    app.refresh("текст 2")            # должен снова спланироваться, не выйти по early-return
    assert app._pending_text == "текст 2"



def test_settings_url_change_forces_refetch(app, tk, monkeypatch, tmp_path):
    """Смена URL в настройках при показанном списке - следующая кнопка качает, не перепроверяет."""
    fetches = []
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fetches.append(urls) or ([(u("h", 443), "h", 443)], 0, [], 0)))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.proxies = [(u("old", 443), "old", 443)]  # список уже показан
    app._fetched_url = app.custom_url or ""  # список скачан ранее
    app.status = {u("old", 443): True}
    app.custom_url = "https://example.com/new.txt"
    app.load_and_check()
    wait_done(app, tk)
    assert len(fetches) == 1, "новый URL не скачался: клик ушёл в перепроверку"


def test_busy_blocks_buttons_is_isolated(app, tk, monkeypatch):
    """busy-тест не ходит в сеть - живой прогон замокан целиком."""
    fetches = []
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fetches.append(urls) or ([(u("h", 443), "h", 443)], 0, [], 0)))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()  # по пустому списку: полный flow на моках
    wait_done(app, tk)
    pump(tk, 200)
    assert app.busy is False
    assert len(fetches) == 1  # сеть не тронута: fetch только мок


def test_load_prefix_dups_only(app, tk, monkeypatch):
    """Ветка dups>0, bad==0: префикс без «некорректных строк»."""
    fresh = [(u("h", 443), "h", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 0, [], 2))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    text = app.label.cget("text")
    assert "дубликатов отброшено: 2" in text
    assert "некорректных" not in text


def test_refresh_skips_stale_selection_index(app):
    """Ветка old_idx >= len(row_urls): выделение за пределами сжавшегося списка."""
    urls = [u("h0", 443), u("h1", 443)]
    app.proxies = [(x, f"h{i}", 443) for i, x in enumerate(urls)]
    app.status = {x: None for x in urls}
    app.refresh("")
    app._do_refresh()
    app.listbox.selection_set(1)
    # после проверки h1 умер: список сжался до 1 строки, старый индекс 1 стал невалиден
    app.status[urls[1]] = False
    app.refresh("")
    app._do_refresh()  # не должно бросить IndexError
    assert app.listbox.size() == 1
    assert app.listbox.curselection() in ((), (0,))


def test_cached_restore_allows_refetch(app, tk, monkeypatch):
    """После старта из кэша кнопка должна уметь качать свежих,
    а не вечно перепроверять кэш (иначе при 0 живых пользователь заперт)."""
    fetches = []
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fetches.append(urls) or ([(u("fresh", 443), "fresh", 443)], 0, [], 0)))
    monkeypatch.setattr(g, "alive", lambda url: True)
    # эмулируем _restore_cached: список есть, источник не фиксировался
    app.proxies = [(u("cached", 443), "cached", 443)]
    app.status = {u("cached", 443): True}
    app._fetched_url = "<none>"  # так делает __init__/_restore_cached
    app.custom_url = None    # дефолтная конфигурация
    app.load_and_check()
    wait_done(app, tk)
    assert len(fetches) == 1, "после кэш-старта свежая загрузка недостижима"


def test_fetched_url_uses_captured_source(app, tk, monkeypatch):
    """Смена custom_url во время загрузки не портит метку списка.
    Воркер пишет источник, зафиксированный на старте, а не текущий."""
    def slow_fetch(urls):
        import time as _t
        _t.sleep(0.3)  # окно, чтобы главный поток сменил custom_url
        return [(u("h", 443), "h", 443)], 0, [], 0

    monkeypatch.setattr(g, "fetch_all", slow_fetch)
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.custom_url = "https://old.example/a.txt"
    app.load_and_check()
    app.custom_url = "https://new.example/b.txt"  # смена в главном потоке, пока качается
    wait_done(app, tk)
    assert app._fetched_url == "https://old.example/a.txt"  # источник закачки, не текущий
    # следующий клик обязан перекачать: fetched != текущего custom_url
    fetches = []
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fetches.append(urls) or ([(u("n", 443), "n", 443)], 0, [], 0)))
    app.load_and_check()
    wait_done(app, tk)
    assert len(fetches) == 1, "новый URL не скачался после смены"


def test_worker_fetches_captured_source(app, tk, monkeypatch):
    """Смена custom_url во время загрузки - качается СТАРЫЙ URL,
    а не новый (метка и контент согласованы)."""
    fetched = {}

    def spy_fetch(urls):
        fetched.update(urls)
        import time as _t
        _t.sleep(0.3)
        return [(u("h", 443), "h", 443)], 0, [], 0

    monkeypatch.setattr(g, "fetch_all", spy_fetch)
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.custom_url = "https://old.example/a.txt"
    app.load_and_check()
    app.custom_url = "https://new.example/b.txt"  # смена в главном потоке
    wait_done(app, tk)
    assert fetched == {"свой": "https://old.example/a.txt"}, "воркер скачал не захваченный источник"


def test_log_traceback_swallows_permission_error(app, tmp_path, monkeypatch):
    """read-only каталог не должен ронять логгер (busy замёрз бы)."""
    monkeypatch.setattr(g, "ERROR_LOG", "Z:/nonexistent-ro-dir/err.log")
    try:
        g._log_traceback()  # PermissionError/OSError внутри - не должно вылететь
    except OSError as e:
        raise AssertionError(f"логгер не глотает OSError: {e}")


def test_merge_cache_crash_caught_by_catchall(app, tk, monkeypatch):
    """Catch-all в _load_and_check: неожиданный отказ в merge не морозит busy."""
    monkeypatch.setattr(g, "fetch_all", lambda urls: ([(u("h", 443), "h", 443)], 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    # ломаем merge: valid вернёт True, а parse бросит (битый кэш-URL)
    monkeypatch.setattr(g, "valid", lambda url: True)
    monkeypatch.setattr(g, "parse", lambda line: (_ for _ in ()).throw(KeyError("secret")))
    p = tmp_settings(app)
    p.write_text(json.dumps({"last_results": {u("cached", 443): True}}), encoding="utf-8")
    app.custom_url = "https://x.example/l.txt"
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert app.busy is False, "неожиданный отказ в merge не снял busy"
    assert "прервана" in app.label.cget("text").lower()


def test_tk_error_swallows_permission_error(app, tk, tmp_path, monkeypatch):
    """_tk_error в read-only каталоге не каскадит."""
    monkeypatch.setattr(g, "ERROR_LOG", "Z:/nonexistent-ro/err.log")
    try:
        app._tk_error(RuntimeError, RuntimeError("тест"), None)
    except OSError as e:
        raise AssertionError(f"_tk_error роняет: {e}")


def test_refresh_tolerates_mid_pair_assignment(app, tk, monkeypatch):
    """Пара proxies/status присваивается двумя STORE_ATTR; отложенный
    _do_refresh может увидеть новый список со старым статусом - refresh не должен
    падать (status.get с fallback None)."""
    monkeypatch.setattr(g, "fetch_all", lambda urls: ([(u("h", 443), "h", 443)], 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    # симулируем окно между STORE_ATTR: новый список, старый статус
    app.proxies, app.status = [(u("new", 443), "new", 443)], app.status
    app._do_refresh()  # не должно кинуть KeyError
    assert len(app.row_urls) == 1


def test_dedup_base64_secret_case_sensitive(app, tk, monkeypatch):
    """Base64url-секреты регистрозависимы - AbC и aBc разные прокси,
    дедуп не должен их схлопывать (hex - по-прежнему без учёта регистра)."""
    b64_1 = "AbCdEfGhIjKl"          # 12 байт base64url
    b64_2 = "aBcDeFgHiJkL"          # та же длина, другой регистр -> другой секрет
    url1 = u("srv", 443).replace("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", b64_1)
    url2 = u("srv", 443).replace("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", b64_2)
    monkeypatch.setattr(g, "fetch_all", lambda urls: ([(url1, "srv", 443), (url2, "srv", 443)], 0, [], 0))
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert len(app.proxies) == 2, "base64url-секреты схлопнуты дедупом: живой потерян"


def test_selection_follows_proxy_after_resort(app, tk, monkeypatch):
    """Селект по url, а не по индексу - сортировка alive-first
    двигает строки, селект не должен переезжать на другого прокси."""
    urls = [u(f"s{i}", 443) for i in range(3)]
    monkeypatch.setattr(g, "fetch_all", lambda src: ([(u_, f"s{i}", 443) for i, u_ in enumerate(urls)], 0, [], 0))
    states = {urls[0]: False, urls[1]: None, urls[2]: None}
    monkeypatch.setattr(g, "alive", lambda url: states[url])
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    # выбран непроверенный urls[1] (внизу списка, живых нет)
    idx = app.row_urls.index(urls[1])
    app.listbox.selection_clear(0, "end")
    app.listbox.selection_set(idx)
    # s0 ожил: сортировка поднимет его, строки сдвинутся
    states[urls[0]] = True
    app.refresh("")
    app._do_refresh()
    new_idx = app.row_urls.index(urls[1])
    selected = app.listbox.curselection()
    assert selected and selected[0] == new_idx, "селект переехал на другого прокси"


def test_failed_sources_allow_retry(app, tk, monkeypatch):
    """При упавших источниках следующий клик качает заново,
    а не уходит в перепроверку (иначе дотянуть упавшие нельзя)."""
    calls = []
    def fake_fetch(urls):
        calls.append(list(urls))
        if len(calls) == 1:
            return [(u("h", 443), "h", 443)], 0, [("zakky8", "HTTP Error 503")], 0
        return [(u("h", 443), "h", 443)], 0, [], 0
    monkeypatch.setattr(g, "fetch_all", fake_fetch)
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert "повторить упавшие" in app.label.cget("text")
    app.load_and_check()  # второй клик: должен снова качать, не перепроверять
    wait_done(app, tk)
    pump(tk, 200)
    assert len(calls) == 2, "упавшие источники не перезакачались повторным кликом"


def test_process_exits_after_mainloop():
    """os._exit(0) после mainloop - не-daemon воркеры не держат процесс
    (старт GUI переехал в main(): проверяем хвост исходника после mainloop)."""
    src = open(g.__file__, encoding="utf-8").read()
    tail = src.split("root.mainloop()", 1)[1]
    assert "os._exit(0)" in tail, "нет os._exit после mainloop"


def test_zero_alive_next_click_refetches(app, tk, monkeypatch):
    """0 живых из N - следующий клик перекачивает источники,
    а не вечно перепроверяет мёртвый список."""
    calls = []
    def fake_fetch(urls):
        calls.append(list(urls))
        return [(u(f"s{i}", 443), f"s{i}", 443) for i in range(3)], 0, [], 0
    monkeypatch.setattr(g, "fetch_all", fake_fetch)
    monkeypatch.setattr(g, "alive", lambda url: False)  # всё мертво
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert "живых 0" in app.label.cget("text")
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    assert len(calls) == 2, "0 живых: клик не перекачал источники (ушёл в перепроверку)"


def test_drain_survives_close_mid_tick(app, tk, monkeypatch):
    """TclError/RuntimeError в reschedule _drain не пишет трейсбек."""
    calls = []
    def boom_after(ms, fn, *a):
        calls.append(fn.__name__)
        if fn.__name__ == "_drain":
            raise RuntimeError("invalid command name")  # Tcl мёртв
        raise RuntimeError("only drain dies")
    monkeypatch.setattr(app.root, "after", boom_after)
    app._q.put((0, lambda: None, ()))
    try:
        app._drain()
    except RuntimeError as e:
        raise AssertionError(f"reschedule не защищён: {e}")



def test_gc_disabled_session_wide():
    """gc выключен на весь сеанс: цикл в фоновом потоке при живом Tcl роняет интерпретатор."""
    import gc as _gc
    assert not _gc.isenabled()


# ---------- выбор языка интерфейса ----------

def find_radio(win, text):
    """Радиокнопка с подписью text в дереве win (лежат внутри Frame настроек)."""
    for child in win.winfo_children():
        if isinstance(child, g.Radiobutton):
            if child.cget("text") == text:
                return child
        else:
            found = find_radio(child, text)
            if found is not None:
                return found
    return None


def click_radio(win, text):
    """Выбрать радиокнопку с подписью text."""
    rb = find_radio(win, text)
    assert rb is not None, f"радиокнопка {text!r} не найдена"
    rb.invoke()


def test_en_dictionary_covers_widgets(app):
    """tr() переводит видимые тексты по словарю; неизвестная строка остаётся русской."""
    app._apply_lang("en")
    assert g.tr("1. Проверить прокси") == "1. Check proxies"
    assert g.tr("Настройки") == "Settings"
    assert g.tr("строки нет в словаре") == "строки нет в словаре"
    assert app.btn_load.cget("text") == "1. Check proxies"
    assert app.btn_enable.cget("text") == "2. Enable"
    assert app.btn_settings.cget("text") == "Settings"
    assert app.label.cget("text").startswith('Click "1. Check proxies"')
    app._apply_lang("ru")
    assert g.tr("1. Проверить прокси") == "1. Проверить прокси"
    assert app.label.cget("text").startswith("Нажмите")


def test_switch_language_retexts_list_marks(app):
    """Метки списка и подсказка переезжают на новый язык без перезапуска."""
    url = u("h", 443)
    app.proxies = [(url, "h", 443)]
    app.status = {url: True}
    app.refresh("")
    app._do_refresh()
    assert app.listbox.get(0).startswith("[живой]")
    app._apply_lang("en")
    assert app.listbox.get(0).startswith("[alive]")
    app._apply_lang("ru")
    assert app.listbox.get(0).startswith("[живой]")


def test_help_window_follows_language(app):
    """Справка: отдельные тексты RU и EN, открытое окно перетекстуется."""
    assert g.help_text() == g.HELP
    app.show_help()
    assert "ЧТО ДЕЛАЕТ ЭТА ПРОГРАММА" in app._help_text.get("1.0", "end")
    app._apply_lang("en")
    assert app._help_win.title() == "Help"
    assert "WHAT THIS PROGRAM DOES" in app._help_text.get("1.0", "end")
    assert app._help_close.cget("text") == "Close"
    app._help_win.destroy()
    app._apply_lang("ru")  # закрытая справка: перетекстовка молчит
    assert g.help_text() == g.HELP


def test_detect_lang_reads_system_locale(monkeypatch):
    """Авто: ru-локаль - русский, иная - английский, неизвестная и сбой - русский."""
    monkeypatch.setattr(g.locale, "getlocale", lambda *a: ("ru_RU", "cp1251"))
    assert g.detect_lang() == "ru"
    monkeypatch.setattr(g.locale, "getlocale", lambda *a: ("Russian_Russia", "utf8"))
    assert g.detect_lang() == "ru"
    monkeypatch.setattr(g.locale, "getlocale", lambda *a: ("en_US", "utf8"))
    assert g.detect_lang() == "en"
    monkeypatch.setattr(g.locale, "getlocale", lambda *a: (None, None))
    assert g.detect_lang() == "ru"

    def broken(*a):
        raise ValueError("unknown locale: 1")

    monkeypatch.setattr(g.locale, "getlocale", broken)
    assert g.detect_lang() == "ru"


def test_language_from_settings_at_startup(app, tk, tmp_path, monkeypatch):
    """Поле lang в настройках поднимает интерфейс английским."""
    p = tmp_path / "lang.json"
    p.write_text(json.dumps({"lang": "en"}), encoding="utf-8")
    monkeypatch.setattr(g, "SETTINGS", str(p))
    app2 = g.App(new_root())
    try:
        assert app2.lang == "en"
        assert app2.btn_load.cget("text") == "1. Check proxies"
        assert app2.label.cget("text").startswith('Click "1. Check proxies"')
    finally:
        app2._on_close()


def test_settings_language_switches_saves_and_applies(app, tk):
    """Переключатель языка: сохраняется в поле lang и применяется сразу."""
    p = tmp_settings(app)
    app.show_settings()
    win = app._settings_win
    assert win.title() == "Настройки"
    click_radio(win, "English")
    click_button(win, "Сохранить")
    assert app.lang == "en"
    assert app.btn_load.cget("text") == "1. Check proxies"
    assert json.loads(p.read_text(encoding="utf-8"))["lang"] == "en"
    # Авто: поле lang убирается, язык решает локаль (в тестах русская)
    app.show_settings()
    assert app._settings_win.title() == "Settings"
    click_radio(app._settings_win, "Auto")
    click_button(app._settings_win, "Save")
    assert app.lang is None
    assert "lang" not in json.loads(p.read_text(encoding="utf-8"))
    assert app.btn_load.cget("text") == "1. Проверить прокси"


def test_settings_language_auto_uses_locale(app, tk, monkeypatch):
    """Авто с английской локалью: после сохранения интерфейс английский."""
    monkeypatch.setattr(g.locale, "getlocale", lambda *a: ("en_GB", "utf8"))
    app.show_settings()
    click_radio(app._settings_win, "Авто")
    click_button(app._settings_win, "Сохранить")
    assert app.lang is None
    assert app.btn_load.cget("text") == "1. Check proxies"


def test_en_status_template_with_numbers(app, tk, monkeypatch):
    """EN-шаблоны статусов с числами: счетчики загрузки и итог проверки."""
    app._apply_lang("en")
    fresh = [(u("live", 443), "live", 443), (u("dead", 443), "dead", 443)]
    monkeypatch.setattr(g, "fetch_all", lambda urls: (fresh, 7, [], 2))
    monkeypatch.setattr(g, "alive", lambda url: "server=live&" in url)
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    text = app.label.cget("text")
    assert "Loaded 2 proxies" in text
    assert "duplicates dropped: 2" in text
    assert "malformed lines: 7" in text
    assert "Done: 1 of 2 alive" in text
    assert "2. Enable" in text


def test_en_hints_and_clipboard_fallback(app, tk, monkeypatch):
    """EN-подсказки enable: пустой список, нет живых, буфер при сбое tg://."""
    app._apply_lang("en")
    app.enable()
    assert app.label.cget("text") == 'Click "1. Check proxies" first'
    url = u("h", 443)
    app.proxies = [(url, "h", 443)]
    app.status = {url: False}
    app.enable()
    assert app.label.cget("text") == 'No live proxies - click "1. Check proxies" first'
    app.status = {url: True}
    monkeypatch.setattr(g.webbrowser, "open", lambda link: False)
    app.enable()
    assert "clipboard" in app.label.cget("text")


# ---------- проверка обновлений из настроек ----------

def find_button(win, text):
    """Кнопка с подписью text в дереве win (кнопки обновлений лежат внутри Frame).
    Скрытая кнопка релизов после pack_forget остаётся виджетом, но не в менеджере
    геометрии - её не считаем видимой."""
    for child in win.winfo_children():
        if (isinstance(child, g.Button) and child.cget("text") == text
                and child.winfo_manager()):
            return child
        found = find_button(child, text)
        if found is not None:
            return found
    return None


def widget_texts(win):
    """Подписи text всех виджетов дерева win (статусные строки обновлений)."""
    out = []
    for child in win.winfo_children():
        try:
            out.append(child.cget("text"))
        except Exception:  # noqa: BLE001 - у Frame/Entry опции text нет
            pass
        out.extend(widget_texts(child))
    return out


def test_settings_update_check_thread_locks_button(app, tk, monkeypatch):
    """Проверка в фоне: кнопка disabled до результата, повторный клик её не запускает."""
    started, release = threading.Event(), threading.Event()
    calls, threads = [], []

    def fake_check():
        calls.append(1)
        threads.append(threading.current_thread())
        started.set()
        release.wait(5)
        return ("newer", "v9.9.9")

    monkeypatch.setattr(g, "check_for_updates", fake_check)
    app.show_settings()
    win = app._settings_win
    assert calls == []  # открытие окна ничего не шлёт: проверка строго по нажатию
    btn = find_button(win, "Проверить обновления")
    assert btn is not None
    btn.invoke()
    assert started.wait(5)
    assert threads[0] is not threading.main_thread()  # запрос не в UI-потоке
    assert str(btn.cget("state")) == "disabled"
    assert any("Проверка обновлений..." in t for t in widget_texts(win))
    btn.invoke()  # disabled: команда не зовётся
    pump(tk, 100)
    assert calls == [1]
    release.set()
    pump(tk, 400)
    assert str(btn.cget("state")) == "normal"  # результат доехал через _after
    assert any("Доступна версия v9.9.9" in t for t in widget_texts(win))
    assert find_button(win, "Открыть страницу релизов") is not None
    win.destroy()


@pytest.mark.parametrize("result,expected", [
    (("current", None), "У вас последняя версия"),
    (("error", None), "Не удалось проверить обновления"),
])
def test_settings_update_statuses(app, tk, monkeypatch, result, expected):
    """Итоги «последняя версия» и «не удалось»: строка результата, кнопки релизов нет."""
    monkeypatch.setattr(g, "check_for_updates", lambda: result)
    app.show_settings()
    win = app._settings_win
    find_button(win, "Проверить обновления").invoke()
    pump(tk, 400)
    assert expected in widget_texts(win)
    assert find_button(win, "Открыть страницу релизов") is None
    win.destroy()


def test_settings_update_recheck_hides_stale_release_button(app, tk, monkeypatch):
    """Повторная проверка прячет прошлую кнопку релизов: сразу при старте и по итогу."""
    monkeypatch.setattr(g, "check_for_updates", lambda: ("newer", "v9.9.9"))
    app.show_settings()
    win = app._settings_win
    btn = find_button(win, "Проверить обновления")
    btn.invoke()
    pump(tk, 400)
    assert find_button(win, "Открыть страницу релизов") is not None
    release = threading.Event()

    def slow():
        release.wait(5)
        return ("current", None)

    monkeypatch.setattr(g, "check_for_updates", slow)
    btn.invoke()
    assert find_button(win, "Открыть страницу релизов") is None  # снята на старте проверки
    release.set()
    pump(tk, 400)
    assert "У вас последняя версия" in widget_texts(win)
    win.destroy()


def test_settings_update_open_releases_uses_browser(app, tk, monkeypatch):
    """Кнопка релизов отдаёт фиксированный URL в webbrowser; сбой браузера - подсказка."""
    monkeypatch.setattr(g, "check_for_updates", lambda: ("newer", "v9.9.9"))
    opened = []
    monkeypatch.setattr(g.webbrowser, "open", lambda url: opened.append(url) or True)
    app.show_settings()
    win = app._settings_win
    find_button(win, "Проверить обновления").invoke()
    pump(tk, 400)
    find_button(win, "Открыть страницу релизов").invoke()
    assert opened == [g.RELEASES_URL]
    # сбой открытия (False и исключение) одинаково: подсказка, приложение живо
    monkeypatch.setattr(g.webbrowser, "open", lambda url: False)
    find_button(win, "Открыть страницу релизов").invoke()
    assert "Не удалось открыть страницу релизов" in widget_texts(win)

    def boom(url):
        raise RuntimeError("нет браузера")

    monkeypatch.setattr(g.webbrowser, "open", boom)
    find_button(win, "Открыть страницу релизов").invoke()  # не должно упасть
    win.destroy()


def test_settings_update_result_after_window_closed_is_safe(app, tk, monkeypatch):
    """Окно закрыли во время проверки: результат приходит в никуда, приложение живо."""
    release = threading.Event()

    def slow():
        release.wait(5)
        return ("newer", "v9.9.9")

    monkeypatch.setattr(g, "check_for_updates", slow)
    app.show_settings()
    find_button(app._settings_win, "Проверить обновления").invoke()
    app._settings_win.destroy()
    release.set()
    pump(tk, 400)  # upd_done по уничтоженному окну: без исключений
    app.show_settings()  # окно и проверка работают дальше
    win2 = app._settings_win
    monkeypatch.setattr(g, "check_for_updates", lambda: ("current", None))
    find_button(win2, "Проверить обновления").invoke()
    pump(tk, 400)
    assert "У вас последняя версия" in widget_texts(win2)
    win2.destroy()


def test_settings_update_no_request_without_click(app, tk, monkeypatch):
    """Обещание «никуда не стучимся»: без нажатия кнопки сеть и проверка не зовутся."""
    calls = []

    def bomb(*a, **kw):
        raise AssertionError("сетевой запрос без нажатия кнопки")

    monkeypatch.setattr(g.rq, "urlopen", bomb)
    monkeypatch.setattr(g, "check_for_updates", lambda: calls.append(1) or ("current", None))
    app.show_settings()
    pump(tk, 200)  # окно открыто: стартовых и фоновых опросов нет
    assert calls == []
    win = app._settings_win
    assert any("Версия: " + g.APP_VERSION in t for t in widget_texts(win))
    win.destroy()


def test_settings_update_strings_en(app, tk, monkeypatch):
    """Строки проверки обновлений на английском: окно, ход и результат."""
    g.set_lang("en")
    assert g.tr("Версия: {v}") == "Version: {v}"
    assert g.tr("Проверить обновления") == "Check for updates"
    assert g.tr("Проверка обновлений...") == "Checking for updates..."
    assert g.tr("Доступна версия {tag}") == "Version {tag} is available"
    assert g.tr("Открыть страницу релизов") == "Open the releases page"
    assert g.tr("У вас последняя версия") == "You have the latest version"
    assert g.tr("Не удалось проверить обновления") == "Could not check for updates"
    assert g.tr("Не удалось открыть страницу релизов") == "Could not open the releases page"
    monkeypatch.setattr(g, "check_for_updates", lambda: ("newer", "v9.9.9"))
    app.show_settings()
    win = app._settings_win
    assert any("Version: " + g.APP_VERSION in t for t in widget_texts(win))
    find_button(win, "Check for updates").invoke()
    pump(tk, 400)
    assert "Version v9.9.9 is available" in widget_texts(win)
    assert find_button(win, "Open the releases page") is not None
    win.destroy()
