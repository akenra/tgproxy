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

pytestmark = pytest.mark.usefixtures("app")


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


@pytest.fixture()
def app(tk, tmp_path):
    """App с SETTINGS на временный файл: кэш-тесты не мусорят друг другу."""
    import tgproxy_gui as _g
    saved = _g.SETTINGS
    _g.SETTINGS = str(tmp_path / "settings.json")
    return _g.App(tk)


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
        if getattr(child, "cget", lambda k: "")("text") == text:
            child.invoke()
            return
    raise AssertionError(f"кнопка {text!r} не найдена")


# ---------- состояние и busy-защита ----------

def test_initial_state(app):
    assert app.proxies == []
    assert app.busy is False
    assert "1. Загрузить" in app.label.cget("text")


def test_busy_blocks_buttons(app):
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: None}
    app._busy_on()
    assert app.busy is True
    assert str(app.btn_load.cget("state")) == "disabled"
    assert str(app.btn_check.cget("state")) == "disabled"
    assert str(app.btn_enable.cget("state")) == "disabled"
    # повторные load/check/enable игнорируются
    app.load_and_check(); app.check(); app.enable()
    app._busy_off()
    assert app.busy is False
    assert str(app.btn_enable.cget("state")) == "normal"


def test_check_progress_not_animated(app, tk, monkeypatch):
    """Регресс: check() раньше включал indeterminate-анимацию поверх determinate,
    полоса скакала 0-100. В determinate-режиме анимация start() не должна идти."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {app.proxies[0][0]: None}
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.check()
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


def test_check_without_proxies_hints(app):
    app.check()
    assert "Загрузить" in app.label.cget("text")
    assert app.busy is False


def test_enable_without_proxies_hints(app):
    app.enable()
    assert "Загрузить" in app.label.cget("text")


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
    # первый живой, остальные мёртвые; вместо сети - мгновенная заглушка
    monkeypatch.setattr(g, "alive", lambda url: "server=h0&" in url)
    app.check()
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

    def boom(_):
        raise RuntimeError("сеть умерла")

    monkeypatch.setattr(g, "alive", boom)
    app.check()
    for _ in range(500):
        pump(tk, 20)
        if not app.busy:
            break
    assert app.busy is False, "исключение в alive() заблокировало кнопки навсегда"


# ---------- refresh: сортировка и подписи ----------

def test_refresh_sorts_alive_first(app):
    urls = [u("dead", 443), u("alive", 443), u("unknown", 443)]
    app.proxies = [(x, x.split("server=")[1], 443) for x in urls]
    app.status = {urls[0]: False, urls[1]: True, urls[2]: None}
    app.refresh("тест")
    app._do_refresh()  # сразу, не дожидаясь батчинга
    rows = app.listbox.get(0, "end")
    assert rows[0].startswith("[живой]"), rows
    assert rows[1].startswith("[ ?"), rows
    assert rows[2].startswith("[мёртв]"), rows
    assert app.row_urls[0] == urls[1]  # двойной клик откроет именно живого


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
    assert "3. Включить" in text
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


def test_drain_survives_dead_window(app, tk):
    """after() на уничтоженном виджете не роняет _drain."""
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


def test_drain_swallows_after_error_on_destroyed_root(app, tk, monkeypatch):
    """root.after() бросил (окно закрывается): _drain глотает и продолжает поллить."""
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
    monkeypatch.setattr(g, "alive", lambda url: False)
    app.check()
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
    monkeypatch.setattr(g, "alive", lambda url: False)
    app.check()
    wait_done(app, tk)
    pump(tk, 200)
    text = app.label.cget("text")
    assert "Готово: живых 0" in text
    assert "1. Загрузить" in text
