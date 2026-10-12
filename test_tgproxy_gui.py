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


@pytest.fixture()
def app(tk, tmp_path, monkeypatch):
    """App с SETTINGS на временный файл: кэш-тесты не мусорят друг другу.
    monkeypatch сам восстановит значение после теста.
    Замечание: _on_close в тесте destroy'ит фикстурный root - после такого теста
    соседние тесты модуля не живут; держим destroy-тесты на отдельных root."""
    import tgproxy_gui as _g
    monkeypatch.setattr(_g, "SETTINGS", str(tmp_path / "settings.json"))
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
    assert "1. Проверить прокси" in app.label.cget("text")


def test_busy_blocks_buttons(app):
    app.proxies = [(u("h", 443), "h", 443)]
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
    app.load_and_check()
    wait_done(app, tk)
    pump(tk, 200)
    text = app.label.cget("text")
    assert "Готово: живых 0" in text
    assert "источники устарели" in text


# ---------- регрессии ----------

def test_rerun_clears_stale_load_prefix(app, tk, monkeypatch):
    """Перепроверка без скачивания не показывает старый текст «Загружено N прокси»."""
    app.proxies = [(u("h", 443), "h", 443)]
    app.status = {u("h", 443): True}
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
    monkeypatch.setattr(g, "alive", lambda url: True)
    app.load_and_check()
    assert not app.label.cget("text").startswith(". ")


# ---------- регрессии ----------

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


# ---------- регрессии ----------

def test_executor_shutdown_on_close_mid_check(tmp_path, monkeypatch):
    """Закрытие окна посреди проверки не оставляет зомби-процесс:
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
    monkeypatch.setattr(g, "alive", slow_alive)
    app.load_and_check()
    started.wait(2)  # хотя бы одна задача пошла
    app._on_close()
    release.set()
    # executor выключен: очередь задач отменена, невыполненные не гоняются
    assert app._ex._shutdown, "executor не выключен при закрытии окна"
