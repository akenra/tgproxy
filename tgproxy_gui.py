#!/usr/bin/env python3
# tgproxy_gui.py: качает список MTProto-прокси, проверяет полным хендшейком,
# копирует живых в буфер и открывает первого через tg:// (Telegram добавит сам).
# Кроссплатформенно: tkinter + webbrowser, единственная зависимость - mtproxy-checker.
import base64, json, os, queue, re, socket, sys, threading, time, traceback, webbrowser
import concurrent.futures as cf
from concurrent.futures import ThreadPoolExecutor
from tkinter import (Tk, Listbox, Button, Label, Toplevel, Entry, Frame, Text,
                     BOTH, LEFT, RIGHT, W, END, SINGLE, DISABLED, NORMAL)
from tkinter import ttk, Scrollbar
from urllib import request as rq, parse as ps

from mtproxy_checker import CheckOptions, check_proxy

# Предустановленные источники, обходятся все подряд с дедупликацией.
# Ключ - короткое имя для настроек/статуса, значение - URL списка.
PRESETS = {
    "zakky8": "https://zakky8.github.io/mtproto-proxy-pro/all_proxies.txt",
    "dubblebyte": "https://raw.githubusercontent.com/dubblebyte/free-mtproto-proxies/main/all_proxies.txt",
    "soli": "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt",
}
TIMEOUT = 5
OPTS = CheckOptions(connect_timeout=TIMEOUT, response_timeout=TIMEOUT)
HELP = """ЧТО ДЕЛАЕТ ЭТА ПРОГРАММА

Она помогает подключиться к Telegram через публичный MTProto-прокси:
скачивает свежий список прокси, проверяет каждый по-настоящему
(не «порт открыт», а полный хендшейк с Telegram) и подключает вас.

ПОРЯДОК РАБОТЫ: ДВЕ КНОПКИ СЛЕВА НАПРАВО

1. «Проверить прокси» - действует по состоянию:
   - списка ещё нет (первый запуск): скачивает списки прокси
     (по URL из настроек, формат: по одной ссылке на строку,
     https://t.me/proxy?server=ХОСТ&port=ПОРТ&secret=СЕКРЕТ)
     и сразу проверяет. Прошлые живые из кэша доливаются
     к свежему списку и перепроверяются;
   - список уже показан: перепроверяет его без скачивания.
   Битые строки пропускаются, их количество показывается в статусе.
   В списке остаются только живые и ещё непроверенные:
   мёртвые не засоряют экран, воскресший появится снова.

2. «Включить»     - делает два действия:
   - копирует живых в буфер обмена (можно вставить списком в Telegram:
     Настройки -> Данные и память -> Прокси -> Добавить прокси -> Ctrl+V);
   - открывает первого живого напрямую в Telegram (tg://-ссылка),
     появится диалог - нажмите «Включить прокси».

ДОПОЛНИТЕЛЬНО

Двойной клик по строке списка - открыть одиночную ссылку
этого прокси в Telegram.

«Настройки» - URL списка прокси. По умолчанию программа
обходит ВСЕ предустановленные источники (3 проверенных списка,
обновляются автоматически каждые 6-12 часов) и отбрасывает
дубликаты. Можно подставить один свой URL (тогда только он).
Хранится в файле tgproxy_settings.json рядом с программой.

ПРИВАТНОСТЬ

Программа никуда не отправляет ваши данные. Единственный трафик -
запрос списка по URL из настроек и прямые проверки прокси с вашей
машины. Это публичные серверы неизвестных владельцев: не
используйте их для чувствительных данных. Владелец прокси видит,
что вы ходите в Telegram, но не видит содержимое переписки
(шифрование MTProto с секретом, сквозное до серверов Telegram)."""
# под PyInstaller onefile __file__ указывает на temp-каталог, настройки там умрут;
# берём каталог исполняемого файла
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
SETTINGS = os.path.join(APP_DIR, "tgproxy_settings.json")
ERROR_LOG = os.path.join(APP_DIR, "tgproxy_error.log")


def _log_traceback():
    """В windowed-exe stderr нет: traceback в файл рядом с программой.
    Лог не критичнее кэша: в read-only каталоге (Program Files) open() бросил бы
    PermissionError и убил воркер до снятия busy."""
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
    except OSError:
        pass


def parse(line):
    q = ps.parse_qs(ps.urlparse(line.strip()).query)
    return (line.strip(), q["server"][0].rstrip("."), int(q["port"][0]))


def _dedup_key(server, port, secret):
    """Ключ дедупа прокси: hex-секрет регистронезависим, base64url - НЕТ
    (AbC и aBc - разные байты, разные прокси): понижаем регистр только hex."""
    s = secret.lower() if re.fullmatch(r"[0-9a-fA-F]+", secret) else secret
    return (server.lower().rstrip("."), port, s)


def valid(line):
    """Строка - прокси-ссылка ожидаемого формата (server+port+secret)."""
    try:
        _, server, port = parse(line)
        secret = ps.parse_qs(ps.urlparse(line.strip()).query)["secret"][0]
        if not (0 < port < 65536) or not server:
            return False
        # Формат секрета (спека MTProto):
        # 1) hex 32 (16 байт, простой) или hex 64 (dd/ee-префикс faketls);
        # 2) base64url-написание: TG принимает и его, минимум 12 байт после декода
        #    (живые dd-секреты вроде dd10400103324995 дают ровно 12).
        # Формат != живость: мусорный, но декодируемый секрет отсеет alive(), не valid().
        if re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{64}", secret):
            return True
        # нечётная длина из hex-символов это битый hex, а не base64
        if re.fullmatch(r"[0-9a-fA-F]+", secret) and len(secret) % 2:
            return False
        pad = "=" * (-len(secret) % 4)
        try:
            return len(base64.urlsafe_b64decode(secret + pad)) >= 12
        except Exception:
            return False
    except Exception:
        return False


def alive(url):
    """Живой = прошёл полный MTProto-хендшейк (TCP + secret + ответ Telegram).
    Двухфазно: сначала дешёвый TCP-коннект (2.5с), полный хендшейк только у открывших:
    мёртвый по сети хост перестаёт съедать полные 5с response-таймаута."""
    try:
        q = ps.parse_qs(ps.urlsplit(url).query)
        host, port = q["server"][0], int(q["port"][0])
        with socket.create_connection((host, port), timeout=2.5):
            pass
    except Exception:
        return False  # TCP не открылся - хендшейк заведомо не пройдёт
    try:
        return check_proxy(url, OPTS).ok
    except Exception:
        return False


def fetch_all(urls):
    """Качает все списки, дедуп по (host, port, secret). Возврат:
    proxies [(url, server, port)], bad (битые строки), fails [(имя, ошибка)], dups."""
    proxies, bad, fails, dups = [], 0, [], 0
    seen = set()
    for name, url in urls.items():
        try:
            with rq.urlopen(rq.Request(url, headers={"User-Agent": "tgproxy/1.0"}),
                            timeout=30) as r:
                raw = r.read(5 * 1024 * 1024)  # потолок 5МБ: список в 50к строк не нужен,
                # а чужой URL не утянет гигабайты в память
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("utf-8-sig", errors="replace")  # битый UTF-8 не валит источник
        except Exception as e:
            fails.append((name, str(e)))
            continue
        for line in text.split():
            if not valid(line):
                bad += 1
                continue
            p = parse(line)
            key = _dedup_key(p[1], p[2], ps.parse_qs(ps.urlparse(p[0]).query)["secret"][0])
            if key in seen:
                dups += 1
            else:
                seen.add(key)
                proxies.append(p)
    return proxies, bad, fails, dups


def load_settings():
    # чтение под тем же локом, что запись: воркер не увидит усечённый файл
    with _settings_lock:
        return _load_settings_unlocked()


def _load_settings_unlocked():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            return {}
        out = {}
        if isinstance(s.get("list_url"), str):
            out["list_url"] = s["list_url"]
        # кэш результатов прошлой проверки: url -> ok (для приоритета живых при повторе)
        if isinstance(s.get("last_results"), dict):
            out["last_results"] = {k: bool(v) for k, v in s["last_results"].items()
                                   if isinstance(k, str) and isinstance(v, bool)}
        return out
    except Exception:
        return {}


# один писатель settings-файла: и воркер проверки, и настройки GUI идут через него
_settings_lock = threading.Lock()


def _write_settings(data):
    """Атомарная запись: temp-файл + os.replace, обрыв не оставит обрезанный JSON."""
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, SETTINGS)


def save_last_results(status):
    """Сохранить исход проверки: url -> True/False (None не храним).
    Читаем файл под тем же локом, что и пишем: не теряем параллельные правки настроек."""
    with _settings_lock:
        s = _load_settings_unlocked()
        s["last_results"] = {u: ok for u, ok in status.items() if ok is not None}
        try:
            _write_settings(s)
        except OSError:
            pass  # кэш приятная мелочь, не повод ронять приложение


class App:
    def __init__(self, root):
        self.root = root
        self._q = queue.Queue()
        self.proxies = []       # [(url, server, port)]
        self._fetched_url = "<none>"  # ещё не качали: кнопка обязана скачать
        self.status = {}        # url -> True/False/None (None = не проверен)
        self.custom_url = load_settings().get("list_url")  # None = все предустановленные
        self.busy = False       # защита от двойных нажатий
        root.title("TG Proxy Checker")
        root.geometry("760x500")
        root.minsize(560, 380)

        self.label = Label(root, wraplength=740, justify=LEFT,
                           text="Нажмите «1. Проверить прокси»: скачает свежий список "
                                     "и покажет только живых. Подробная инструкция - кнопка «?» справа внизу.", anchor=W)
        self.label.pack(fill=BOTH, padx=8, pady=(8, 2))

        self._restore_cached()

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        # показывается только во время фоновой работы (см. _busy_on/_busy_off)
        self.progress.pack_forget()

        self.listbox = Listbox(root, selectmode=SINGLE, exportselection=False)
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True, padx=(8, 0), pady=4)
        sb = Scrollbar(root, command=self.listbox.yview)
        sb.pack(side=RIGHT, fill='y', pady=4)
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.bind("<Double-Button-1>", self.open_selected)
        self.row_urls = []

        bar = Frame(root)
        bar.pack(fill=BOTH, padx=8, pady=(0, 8))
        self.btn_load = Button(bar, text="1. Проверить прокси", width=20, command=self.load_and_check)
        self.btn_enable = Button(bar, text="2. Включить", width=12, command=self.enable)
        self.btn_load.pack(side=LEFT, padx=2)
        self.btn_enable.pack(side=LEFT, padx=2)
        Button(bar, text="?", width=3, command=self.show_help).pack(side=RIGHT, padx=2)
        Button(bar, text="Настройки", width=10, command=self.show_settings).pack(side=RIGHT, padx=2)
        self.root.after(25, self._drain)  # поллинг очереди колбэков в главном потоке
        # закрытие окна при живом фоновой работе: гасим очередь, чтобы finally в _drain
        # не звал after() на уничтоженном root (traceback ушёл бы в никуда в windowed-exe)
        self._closing = False
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.report_callback_exception = self._tk_error  # windowed-exe без stderr: пишем в лог

    def _on_close(self):
        self._closing = True
        # executor выключаем с отменой очереди: иначе процесс после закрытия окна
        # джойнит сотни задач, десятки секунд оставаясь невидимым
        if getattr(self, "_ex", None) is not None:
            self._ex.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def _restore_cached(self):
        """Старт из кэша прошлой проверки: список виден сразу, сеть не трогаем."""
        cached = load_settings().get("last_results", {})
        rows = []
        for url, ok in cached.items():
            if valid(url):
                rows.append((url, *parse(url)[1:]))
        if not rows:
            return
        # живые сверху: сортировка та же, что в _do_refresh
        rows.sort(key=lambda p: not cached[p[0]])
        self.proxies = rows
        self.status = {url: cached[url] for url, _, _ in rows}
        visible = [url for url, _, _ in rows if self.status[url] is not False]
        self.refresh(f"Восстановлено из кэша прошлой проверки: прокси в списке {len(visible)}, "
                     f"живых {sum(1 for v in self.status.values() if v)}. "
                     f"Нажмите «1. Проверить прокси», чтобы перепроверить их")

    def _tk_error(self, exc_type, exc_value, tb):
        # исключение в Tk-колбэке (windowed-exe без stderr): в файл, не молча.
        # логгер не критичнее обрабатываемой ошибки: read-only каталог не каскадит
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                traceback.print_exception(exc_type, exc_value, tb, file=f)
        except OSError:
            pass

    def set(self, text):
        self.label.config(text=text)

    def _after(self, ms, fn, *args):
        """Колбэк из любого потока. Tcl бывает не-threaded (Python 3.14 official):
        root.after() из воркера кидает RuntimeError, поэтому маршируем через очередь,
        а исполняет колбэки _drain() в главном потоке (запускается в __init__)."""
        self._q.put((ms, fn, args))

    def _drain(self):
        # reschedule в finally: если after() на колбэке бросил (окно закрывается),
        # поллинг всё равно продолжится, иначе очередь замрёт навсегда.
        # _closing: после WM_DELETE_WINDOW тики не планируются (root уничтожен).
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                ms, fn, args = self._q.get_nowait()
                try:
                    self.root.after(ms, fn, *args)
                except Exception:
                    # колбэк потерян: батчинг-флаг (например _refresh_pending у
                    # _do_refresh) надо сбросить, иначе refresh() замрёт навсегда
                    if fn == self._do_refresh:
                        self._refresh_pending = False
                    _log_traceback()  # в windowed-сборке терять колбэк молча нельзя
        except queue.Empty:
            pass
        finally:
            self.root.after(25, self._drain)

    def _busy_on(self, indeterminate=True):
        """Защита от дурака: пока фоновая работа идёт, кнопки выключены.
        indeterminate=True: бегущая полоса (проценты неизвестны).
        indeterminate=False: статичная полоса, value двигает _check()."""
        self.busy = True
        if indeterminate:
            self.progress.config(mode="indeterminate")
            self.progress.pack(fill="x", padx=8, pady=(0, 2), before=self.listbox)
            self.progress.start(40)  # бегущая полоса: видно, что не зависли
        for b in (self.btn_load, self.btn_enable):
            b.config(state=DISABLED)

    def _busy_off(self):
        self.busy = False
        # stop() только в indeterminate: в determinate он сбрасывает value в 0
        # (и обнулил бы показанный итог 100%). Остаточный after-таймер indeterminate
        # гасим раньше смены режима, в _run_check - там это безопасно.
        if str(self.progress.cget("mode")) == "indeterminate":
            self.progress.stop()
        self.progress.pack_forget()
        for b in (self.btn_load, self.btn_enable):
            b.config(state=NORMAL)

    def load_and_check(self):
        """Кнопка проверки работает по состоянию. Нет списка - скачать и проверить;
        список уже есть - перепроверить без скачивания. custom_url, изменённый
        после прошлой загрузки, форсирует перекачку: иначе новый источник
        заработал бы только после перезапуска."""
        if self.busy:
            return
        fetched_now = self.custom_url or ""
        if self.proxies and self._fetched_url == fetched_now and fetched_now != "<none>":
            self._load_prefix = ""  # перепроверка без скачивания: старый текст «Загружено N» устарел
            self._busy_on(indeterminate=False)
            self._run_check(len(self.proxies))
            return
        self._busy_on()
        src = self.custom_url or "все предустановленные источники"
        self.set(f"Загрузка {src} ...")
        # источник фиксируем до старта потока: пользователь может сменить URL
        # в настройках, пока качается, - список должен остаться помечен старым
        src_url = self.custom_url
        threading.Thread(target=self._load_and_check, args=(src_url,), daemon=True).start()

    def _load_and_check(self, src_url):
        try:
            urls = {"свой": src_url} if src_url else PRESETS
            try:
                proxies, bad, fails, dups = fetch_all(urls)
            except Exception as e:
                self._after(0, self._busy_off)
                self._after(0, self.set, f"Ошибка загрузки: {e}. Проверьте интернет и URL в настройках")
                return
            # оба присваивания атомарны для читателя: refresh ставится в очередь после них,
            # а исполняется в главном потоке - новые proxies всегда приходят вместе со status
            if not proxies:
                self._after(0, self._busy_off)
                self._after(0, self.refresh,
                            "Списки пусты или в неправильном формате - проверьте интернет и URL в настройках")
                return
            # прошлые живые из кэша доливаем к свежим и перепроверяем: могли ожить/умереть.
            # дедуп по тому же ключу, что в fetch_all: кэш может хранить другое написание
            # того же прокси (server=h. vs server=h)
            cached = load_settings().get("last_results", {})
            seen = {_dedup_key(p[1], p[2], ps.parse_qs(ps.urlparse(p[0]).query)["secret"][0])
                    for p in proxies}
            merged = list(proxies)
            for url, ok in cached.items():
                if ok and valid(url):
                    _, server, port = parse(url)
                    secret = ps.parse_qs(ps.urlparse(url).query)["secret"][0]
                    ckey = _dedup_key(server, port, secret)
                    if ckey not in seen:
                        seen.add(ckey)
                        merged.append((url, server, port))
            # присваивание пары - это два STORE_ATTR: отложенный _do_refresh прошлого
            # прогона может увидеть новый список со старым статусом (транзиентное
            # «[ ? ]» на один тик, самоисцеляется через 100мс). Снапшот-кортеж не
            # помогает - те же два STORE_ATTR; единый _data потребовал бы
            # рефакторинга всех читателей ради 100мс-мигания
            self.proxies, self.status = merged, {u: None for u, _, _ in merged}
            self._fetched_url = src_url or ""  # источник, по которому реально качали
            if fails:
                # упавшие источники надо дотянуть: следующий клик должен качать заново,
                # а не уходить в перепроверку скачанного (иначе недостижимы без правки настроек)
                self._fetched_url = "<retry-needed>"
            total = len(self.proxies)
            self._load_prefix = f"Загружено {len(proxies)} прокси"
            if fails:
                self._load_prefix += " (нажмите ещё раз, чтобы повторить упавшие источники)"
            if dups:
                self._load_prefix += f" (дубликатов отброшено: {dups}"
                if bad:
                    self._load_prefix += f", некорректных строк: {bad}"
                self._load_prefix += ")"
            elif bad:
                self._load_prefix += f" (некорректных строк: {bad})"
            for name, err in fails:
                self._load_prefix += f"; [{name}: {err}]"
            # дальше общий хвост проверки: полоса, статус, тот же финальный текст
            self._after(0, self._run_check, total)
        except Exception:
            _log_traceback()
            self._after(0, self._busy_off)
            self._after(0, self.set, "Загрузка прервана внутренней ошибкой (см. лог) - повторите")

    def _run_check(self, total):
        """Проверка: determinate-полоса по числу проверенных, живые вверх, кэш по итогам."""
        # погасить indeterminate-анимацию скачивания: её таймер Autoincrement иначе
        # продолжает step() в determinate-режиме, борясь с value из воркера
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=total, value=0)
        self.progress.pack(fill="x", padx=8, pady=(0, 2), before=self.listbox)
        p = getattr(self, "_load_prefix", "")
        pre = f"{p}. " if p else ""
        self.set(f"{pre}Проверка (полный MTProto-хендшейк)...")
        # executor живёт в главном потоке: присвоение self._ex до старта воркера,
        # иначе _on_close в окне между стартом потока и присвоением промахнётся
        self._ex = ThreadPoolExecutor(128)
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        # прошлые живые проверяем первыми: пользователь скорее всего ждёт их
        last = load_settings().get("last_results", {})
        urls = [u for u, _, _ in self.proxies]
        urls.sort(key=lambda u: not last.get(u, False))
        total = len(urls)
        done = alive_n = 0
        load_msg = getattr(self, "_load_prefix", "")  # счётчики загрузки из load_and_check
        try:
            with self._ex:
                # Python 3.14: map сабмитит лениво - если окно закрыли и executor
                # уже shutdown, итерация бросит RuntimeError; это штатный выход
                try:
                    results = self._ex.map(alive, urls)
                    for url, ok in zip(urls, results):
                        self.status[url] = ok
                        done += 1
                        alive_n += ok
                        self._after(0, lambda v=done: self.progress.config(value=v))
                        self._after(0, self.refresh,
                                        f"{load_msg}{' ' if load_msg else ''}Проверено {done}/{total}, живых {alive_n}")
                except RuntimeError:
                    pass  # executor выключен закрытием окна: выходим через finally
                except cf.CancelledError:
                    pass  # отменённые фьючерсы (BaseException с 3.8): штатный выход
        finally:
            # без finally исключение в alive() оставило бы кнопки навсегда выключенными.
            # кэш пишем всегда: None фильтруется в save_last_results, частичный прогон
            # не теряет проверенных живых при следующем старте
            save_last_results(self.status)
            self._after(0, self._busy_off)
            if done == total:
                tail = f" {load_msg}" if load_msg else ""
                if alive_n == 0 and total > 0:
                    self._after(0, self.refresh,
                                f"Готово: живых 0 из {total}.{tail} Возможно, все источники устарели - "
                                f"попробуйте позже или свой URL в настройках")
                else:
                    self._after(0, self.refresh,
                                f"Готово: живых {alive_n} из {total}.{tail} Теперь «2. Включить»")
            else:  # оборвалось исключением: не выдаём проверку за завершённую
                self._after(0, self.refresh,
                            f"Проверка прервана на {done}/{total}, живых {alive_n}. Повторите «1. Проверить прокси»")

    def enable(self):
        if self.busy:
            return
        if not self.proxies:
            self.set("Сначала «1. Проверить прокси»")
            return
        # порядок загрузки: первый живой = верхний [живой] в списке, как видит пользователь
        good = [u for u, _, _ in self.proxies if self.status.get(u)]
        if not good:
            self.set("Нет живых прокси - сначала «1. Проверить прокси»")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(good) + "\n")
        # закрепление в системный буфер: на X11/Wayland буфер живёт, пока жив процесс Tk,
        # поэтому приложение не закрывается само. update_idletasks обрабатывает отложенные
        # задачи Tk, но selection-запросы чужих процессов на X11 обслуживает event loop,
        # поэтому важно, чтобы процесс продолжал жить (mainloop не завершается)
        self.root.update_idletasks()
        # t.me тут открывается браузером (обработчик tg:// есть, t.me у машины нет),
        # поэтому прямиком в протокол Telegram: tg://proxy?...
        tg_url = "tg://proxy?" + ps.urlsplit(good[0]).query
        try:
            opened = webbrowser.open(tg_url)
        except Exception:
            opened = False  # на некоторых платформах бросает, а не возвращает False
        if not opened:
            self.set("Не удалось открыть tg://-ссылку: Telegram не установлен или не ассоциирован. "
                     "Список живых уже в буфере - вставьте вручную: Настройки -> Данные и память -> "
                     "Прокси -> Добавить, Ctrl+V.")
            return
        self.set(f"Живых {len(good)}: первый открыт в Telegram - подтвердите «Включить прокси» "
                 f"в появившемся диалоге. Весь список уже в буфере.")

    def refresh(self, text):
        # батчинг: не чаще раза в 100мс, иначе при 128 потоках список часто мигает
        now = time.monotonic()
        self._pending_text = text
        if getattr(self, "_refresh_pending", False):
            return
        self._refresh_pending = True
        self._after(100, self._do_refresh)

    def _do_refresh(self):
        self._refresh_pending = False
        text = getattr(self, "_pending_text", "")
        # сохраняем скролл и выделение: во время проверки refresh тикает каждые 100мс,
        # полный rebuild без восстановления сделал бы просмотр списка невозможным
        yview = self.listbox.yview()
        sel = self.listbox.curselection()
        self.listbox.delete(0, END)
        # показываем только живых и ещё непроверенных: мёртвые - визуальный шум,
        # воскресший (False -> True при перепроверке) снова появляется сам.
        # статус берём один раз на элемент: воркер может сменить его между
        # фильтром и сортировкой, повторные get дадут рассинхрон (KeyError)
        rows = [(p, self.status.get(p[0])) for p in self.proxies]
        rows = [(p, st) for p, st in rows if st is not False]
        rows.sort(key=lambda t: 0 if t[1] else 1)
        # выделение переносим по url, а не по индексу: сортировка alive-first
        # двигает строки, селект по индексу «переезжал» на другого прокси
        sel_urls = {self.row_urls[i] for i in sel if i < len(self.row_urls)}
        self.row_urls = [p[0] for p, _ in rows]
        for (url, server, port), st in rows:
            mark = "[живой] " if st else "[ ?    ] "
            self.listbox.insert(END, f"{mark}{server}:{port}")
            if st:
                self.listbox.itemconfig(END, fg="#1a7f37")
        for url in sel_urls:
            if url in self.row_urls:
                self.listbox.selection_set(self.row_urls.index(url))
        if yview and yview != (0.0, 1.0):
            self.listbox.yview_moveto(yview[0])
        self.set(text)

    def open_selected(self, _):
        if self.busy:
            return  # список пересортируется live, индексы плывут
        sel = self.listbox.curselection()
        if sel:
            # как и в enable(): t.me-обработчика может не быть, открываем напрямую в Telegram
            tg_url = "tg://proxy?" + ps.urlsplit(self.row_urls[sel[0]]).query
            try:
                opened = webbrowser.open(tg_url)
            except Exception:
                opened = False
            if not opened:
                self.set("Не удалось открыть tg://-ссылку: Telegram не ассоциирован. "
                         "Ссылка прокси: " + self.row_urls[sel[0]])

    def show_help(self):
        if getattr(self, "_help_win", None) is not None and self._help_win.winfo_exists():
            self._help_win.lift()
            return
        win = self._help_win = Toplevel(self.root)
        win.title("Справка")
        win.geometry("680x600")
        # ponytail: фиксированное окно. Текст HELP длиннее - нужен скролл,
        # не раздувать окно бесконечно. Перепишешь HELP длиннее - поднимай высоту.
        body = Frame(win)
        body.pack(fill=BOTH, expand=True, padx=12, pady=12)
        txt = Text(body, wrap="word", borderwidth=0, highlightthickness=0)
        sb = Scrollbar(body, command=txt.yview)
        txt.config(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill="y")
        txt.pack(side=LEFT, fill=BOTH, expand=True)
        txt.insert("1.0", HELP)
        txt.config(state="disabled")  # только чтение
        Button(win, text="Закрыть", command=win.destroy).pack(pady=(0, 12))

    def show_settings(self):
        if getattr(self, "_settings_win", None) is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        win = self._settings_win = Toplevel(self.root)
        win.title("Настройки")
        win.geometry("560x150")
        presets_list = "\n".join(f"  {k}: {v}" for k, v in PRESETS.items())
        Label(win, text="URL списка прокси (формат: t.me/proxy?server=...&port=...&secret=...).\n"
                        "Пустое поле = обойти все предустановленные источники:\n" + presets_list,
              anchor=W, justify=LEFT, wraplength=530).pack(fill=BOTH, padx=12, pady=(12, 2))
        entry = Entry(win)
        entry.insert(0, self.custom_url or "")
        entry.pack(fill=BOTH, padx=12, pady=4)

        def save():
            url = entry.get().strip() or None  # пустое поле = все предустановленные
            if url and not url.lower().startswith(("http://", "https://")):
                self.set("URL должен начинаться с http:// или https:// (или пустое поле = все источники)")
                return
            try:
                # читаем актуальный файл под локом: не затираем кэш, который мог
                # дописать воркер проверки между открытием окна и «Сохранить»
                with _settings_lock:
                    s = _load_settings_unlocked()
                    # пустой URL = сброс на предустановленные, храним только list_url
                    s["list_url"] = url
                    if not url:
                        s.pop("list_url")
                    _write_settings(s)
            except OSError as e:
                self.set(f"Не удалось сохранить настройки: {e}")
                return
            self.custom_url = url
            win.destroy()
            self.set("Источники: все предустановленные. Нажмите «1. Проверить прокси»." if not url
                     else f"URL сохранён: {url}. Нажмите «1. Проверить прокси».")

        Button(win, text="Сохранить", command=save).pack(side=LEFT, padx=12, pady=8)
        Button(win, text="Все предустановленные",
               command=lambda: (entry.delete(0, END), entry.insert(0, ""))).pack(side=LEFT, pady=8)


def selftest():
    good = "https://t.me/proxy?server=x.y.info.&port=7799&secret=" + "a" * 32
    assert valid(good) and parse(good)[1:] == ("x.y.info", 7799)
    assert not valid("https://example.org/")
    assert not valid("https://t.me/proxy?server=x&port=1&secret=short")
    assert not valid("https://t.me/proxy?server=x&port=99999&secret=" + "a" * 32)


if __name__ == "__main__":  # pragma: no cover - исполняется только запуском exe
    selftest()
    root = Tk()
    App(root)
    root.mainloop()
    # воркеры alive() не-daemon: интерпретатор джойнит их до 8с после смерти окна,
    # пользователь видит «приложение не закрылось». State уже сохранён под локом.
    os._exit(0)
