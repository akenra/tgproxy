#!/usr/bin/env python3
# tgproxy_gui.py: качает список MTProto-прокси, проверяет полным хендшейком,
# копирует живых в буфер и открывает первого через tg:// (Telegram добавит сам).
# Кроссплатформенно: tkinter + webbrowser, единственная зависимость - mtproxy-checker.
import base64, json, os, queue, re, sys, threading, time, traceback, webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import (Tk, Listbox, Button, Label, Toplevel, Entry, Frame,
                     BOTH, LEFT, RIGHT, W, END, SINGLE, DISABLED, NORMAL, NW)
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
DEFAULT_URL = PRESETS["zakky8"]
TIMEOUT = 5
OPTS = CheckOptions(connect_timeout=TIMEOUT, response_timeout=TIMEOUT)
HELP = """ЧТО ДЕЛАЕТ ЭТА ПРОГРАММА

Она помогает подключиться к Telegram через публичный MTProto-прокси:
скачивает свежий список прокси, проверяет каждый по-настоящему
(не «порт открыт», а полный хендшейк с Telegram) и подключает вас.

ПОРЯДОК РАБОТЫ: ТРИ КНОПКИ СЛЕВА НАПРАВО

1. «Загрузить»  - скачивает список прокси по URL из настроек.
   Ожидаемый формат: по одной ссылке на строку,
   https://t.me/proxy?server=ХОСТ&port=ПОРТ&secret=СЕКРЕТ
   Битые строки пропускаются, их количество показывается в статусе.

2. «Проверить»  - гоняет все адреса через полную проверку
   (TCP + MTProto-хендшейк с секретом + ответ Telegram).
   Занимает около минуты. По ходу: зелёные сверху - живые,
   красные внизу - мёртвые, серые - ещё не проверены.

3. «Включить»   - делает два действия:
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
    """В windowed-exe stderr нет: traceback в файл рядом с программой."""
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        traceback.print_exc(file=f)


def parse(line):
    q = ps.parse_qs(ps.urlparse(line.strip()).query)
    return (line.strip(), q["server"][0].rstrip("."), int(q["port"][0]))


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
    """Живой = прошёл полный MTProto-хендшейк (TCP + secret + ответ Telegram)."""
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
            with rq.urlopen(url, timeout=30) as r:
                raw = r.read()
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
            key = (p[1], p[2], ps.parse_qs(ps.urlparse(p[0]).query)["secret"][0])
            if key in seen:
                dups += 1
            else:
                seen.add(key)
                proxies.append(p)
    return proxies, bad, fails, dups


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) and isinstance(s.get("list_url"), str) else {}  # защита от рукatenого JSON
    except Exception:
        return {}


class App:
    def __init__(self, root):
        self.root = root
        self._q = queue.Queue()
        self.proxies = []       # [(url, server, port)]
        self.status = {}        # url -> True/False/None (None = не проверен)
        self.custom_url = load_settings().get("list_url")  # None = все предустановленные
        self.busy = False       # защита от двойных нажатий
        root.title("TG Proxy Checker")
        root.geometry("760x500")
        root.minsize(560, 380)

        self.label = Label(root, wraplength=740, justify=LEFT,
                           text="Начните с кнопки «1. Загрузить» слева внизу. "
                                      "Подробная инструкция - кнопка «?» справа внизу.", anchor=W)
        self.label.pack(fill=BOTH, padx=8, pady=(8, 2))

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
        self.btn_load = Button(bar, text="1. Загрузить", width=14, command=self.load)
        self.btn_check = Button(bar, text="2. Проверить", width=14, command=self.check)
        self.btn_enable = Button(bar, text="3. Включить", width=14, command=self.enable)
        self.btn_load.pack(side=LEFT, padx=2)
        self.btn_check.pack(side=LEFT, padx=2)
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
        self.root.destroy()

    def _tk_error(self, exc_type, exc_value, tb):
        # исключение в Tk-колбэке (windowed-exe без stderr): в файл, не молча
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            traceback.print_exception(exc_type, exc_value, tb, file=f)

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
        for b in (self.btn_load, self.btn_check, self.btn_enable):
            b.config(state=DISABLED)

    def _busy_off(self):
        self.busy = False
        # stop() нужен только после start(); на determinate-полосе он сбрасывает value в 0
        if str(self.progress.cget("mode")) == "indeterminate":
            self.progress.stop()
        self.progress.pack_forget()
        for b in (self.btn_load, self.btn_check, self.btn_enable):
            b.config(state=NORMAL)

    def load(self):
        if self.busy:
            return
        self._busy_on()
        src = self.custom_url or "все предустановленные источники"
        self.set(f"Загрузка {src} ...")
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        urls = {"свой": self.custom_url} if self.custom_url else PRESETS
        try:
            proxies, bad, fails, dups = fetch_all(urls)
        except Exception as e:
            self._after(0, self._busy_off)
            self._after(0, self.set, f"Ошибка загрузки: {e}. Проверьте интернет и URL в настройках")
            return
        # оба присваивания атомарны для читателя: refresh ставится в очередь после них,
        # а исполняется в главном потоке - новые proxies всегда приходят вместе со status
        self.proxies = proxies
        self.status = {u: None for u, _, _ in self.proxies}
        msg = f"Загружено {len(self.proxies)} прокси (дубликатов отброшено: {dups}), теперь нажмите «2. Проверить»"
        if bad:
            msg += f" (некорректных строк: {bad})"
        for name, err in fails:
            msg += f"; [{name}: {err}]"
        if not self.proxies:
            msg = "Списки пусты или в неправильном формате - проверьте интернет и URL в настройках"
        self._after(0, self._busy_off)
        self._after(0, self.refresh, msg)

    def check(self):
        if self.busy:
            return
        if not self.proxies:
            self.set("Сначала «1. Загрузить»")
            return
        self._busy_on(indeterminate=False)
        self.progress.config(mode="determinate", maximum=len(self.proxies), value=0)
        self.progress.pack(fill="x", padx=8, pady=(0, 2), before=self.listbox)
        self.set(f"Проверка (полный MTProto-хендшейк), обычно несколько минут...")
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        urls = [u for u, _, _ in self.proxies]
        total = len(urls)
        done = alive_n = 0
        try:
            with ThreadPoolExecutor(32) as ex:
                for url, ok in zip(urls, ex.map(alive, urls)):
                    self.status[url] = ok
                    done += 1
                    alive_n += ok
                    self._after(0, lambda v=done: self.progress.config(value=v))
                    self._after(0, self.refresh,
                                    f"Проверено {done}/{total}, живых {alive_n}")
        finally:
            # без finally исключение в alive() оставило бы кнопки навсегда выключенными
            self._after(0, self._busy_off)
            if done == total:
                self._after(0, self.refresh, f"Готово: живых {alive_n} из {total}. Теперь «3. Включить»")
            else:  # оборвалось исключением: не выдаём проверку за завершённую
                self._after(0, self.refresh,
                            f"Проверка прервана на {done}/{total}, живых {alive_n}. Повторите «2. Проверить»")

    def enable(self):
        if self.busy:
            return
        if not self.proxies:
            self.set("Сначала «1. Загрузить» и «2. Проверить»")
            return
        # порядок загрузки: первый живой = верхний [живой] в списке, как видит пользователь
        good = [u for u, _, _ in self.proxies if self.status.get(u)]
        if not good:
            self.set("Нет живых прокси - сначала «2. Проверить»")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(good) + "\n")
        # закрепление в системный буфер: на X11/Wayland буфер живёт, пока жив процесс Tk,
        # поэтому приложение не закрывается само; update_idletasks обрабатывает запросы
        # выделения от других приложений, не клики (re-entrancy исключена)
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
        # батчинг: не чаще раза в 100мс, иначе при 32 потоках мельтешит
        now = time.monotonic()
        self._pending_text = text
        if getattr(self, "_refresh_pending", False):
            return
        self._refresh_pending = True
        self._after(100, self._do_refresh)

    def _do_refresh(self):
        self._refresh_pending = False
        text = getattr(self, "_pending_text", "")
        self.listbox.delete(0, END)
        # живые сверху, потом непроверенные, мёртвые вниз
        rows = sorted(self.proxies,
                      key=lambda p: {True: 0, None: 1, False: 2}[self.status.get(p[0])])
        self.row_urls = [p[0] for p in rows]
        for url, server, port in rows:
            st = self.status.get(url)
            mark = {True: "[живой] ", False: "[мёртв] ", None: "[ ?    ] "}[st]
            self.listbox.insert(END, f"{mark}{server}:{port}")
            color = {True: "#1a7f37", False: "#c62828", None: ""}[st]
            if color:
                self.listbox.itemconfig(END, fg=color)
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
        win.geometry("680x560")
        Label(win, text=HELP, justify=LEFT, anchor=NW, wraplength=640).pack(fill=BOTH, expand=True, padx=12, pady=12)
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
                with open(SETTINGS, "w", encoding="utf-8") as f:
                    # пустой URL = сброс на предустановленные, храним пустой dict
                    json.dump({"list_url": url} if url else {}, f)
            except OSError as e:
                self.set(f"Не удалось сохранить настройки: {e}")
                return
            self.custom_url = url
            win.destroy()
            self.set("Источники: все предустановленные. Нажмите «1. Загрузить»." if not url
                     else f"URL сохранён: {url}. Нажмите «1. Загрузить».")

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
