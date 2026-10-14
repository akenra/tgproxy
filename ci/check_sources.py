#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельная проверка живости источников прокси (workflow sources-watch).

Качает каждый источник из tgproxy_gui.PRESETS отдельно, считает валидные прокси
логикой приложения (valid/parse/_dedup_key), копит состояние в ci/sources-status.json
и возвращает код 1 при алерте: ошибка скачивания или затяжная пустота (empty_streak
достиг 3 прогонов подряд).

Запуск: python ci/check_sources.py (обновляет JSON и печатает таблицу).
"""
import datetime
import json
import os
import re
import sys
from urllib import parse as ps
from urllib import request as rq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # python ci/check_sources.py: корень репо не в sys.path
from tgproxy_gui import PRESETS, _dedup_key, parse, valid  # noqa: E402

STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources-status.json")
ALERT_STREAK = 3  # пустых прогонов подряд, после которых бьём тревогу
UA = "tgproxy/1.0"  # как в fetch_all: у приложения и проверки одинаковые запросы
TIMEOUT = 30
MAX_BYTES = 5 * 1024 * 1024  # потолок как в fetch_all: чужой URL не утянет гигабайты
ERROR_MAX = 120  # ошибка одной строкой: в JSON и таблице её читает человек


def utc_today():
    """Дата прогона по UTC: расписание workflow тоже UTC, локальная дата раннера врёт."""
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def short_error(exc):
    """Ошибка одной строкой не длиннее ERROR_MAX символов."""
    return " ".join(str(exc).split())[:ERROR_MAX]


def fetch_source(url):
    """Скачать один источник и посчитать уникальные валидные прокси.
    Запрос и разбор - как в tgproxy_gui.fetch_all (UA, таймаут, потолок, снятие
    строк-комментариев, дедуп по _dedup_key); логика разбора не дублируется.
    Возврат: {"status": "ok"|"empty"|"error", "count": int|None, "bytes": int|None,
    "error": str|None}; исключение скачивания - статус error, а не падение скрипта.
    """
    try:
        with rq.urlopen(rq.Request(url, headers={"User-Agent": UA}), timeout=TIMEOUT) as r:
            raw = r.read(MAX_BYTES)
    except Exception as e:
        return {"status": "error", "count": None, "bytes": None, "error": short_error(e)}
    text = raw.decode("utf-8-sig", errors="replace")  # битый UTF-8 не валит источник
    # строки-комментарии шапок отбрасываем до разбора: их токены считались бы битыми
    text = re.sub(r"(?m)^\s*#.*$", "", text)
    seen = set()
    for line in text.split():
        if valid(line):
            p = parse(line)
            seen.add(_dedup_key(p[1], p[2],
                                ps.parse_qs(ps.urlparse(p[0]).query)["secret"][0]))
    return {"status": "ok" if seen else "empty", "count": len(seen), "bytes": len(raw),
            "error": None}


def evaluate(prev, result, today):
    """Состояние источника после прогона (чистая функция, prev не мутируется).
    ok: серия пустот и ошибка сброшены; empty: серия +1, счётчик 0;
    error: только last_error - серию и last_ok не трогаем.
    """
    s = dict(prev) if isinstance(prev, dict) else {}
    s.setdefault("last_ok", None)
    s.setdefault("last_count", None)
    s.setdefault("empty_streak", 0)
    s.setdefault("last_error", None)
    if result["status"] == "ok":
        s.update(last_ok=today, last_count=result["count"], empty_streak=0, last_error=None)
    elif result["status"] == "empty":
        s.update(empty_streak=s["empty_streak"] + 1, last_count=0)
    else:
        s["last_error"] = result["error"]
    return s


def exit_code(results, states):
    """Код возврата прогона: 1 - алерт (ошибка скачивания или серия пустот >= 3)."""
    alert = (any(r["status"] == "error" for r in results.values())
             or any(s.get("empty_streak", 0) >= ALERT_STREAK for s in states.values()))
    return 1 if alert else 0


def load_state(path):
    """Состояние прошлого прогона; нет файла или он битый - пустой словарь."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path, state):
    """Записать состояние; контент не изменился - файл не трогаем (не плодим диффы).
    Формат: sort_keys=True, ensure_ascii=False, LF, финальный перевод строки.
    Возврат: True - записан, False - уже актуален.
    """
    text = json.dumps(state, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(path, encoding="utf-8") as f:
            # текст читается с универсальными переводами строк: CRLF-чек-аут
            # Windows (autocrlf) не считается изменением, в git уходит LF
            if f.read() == text:
                return False
    except OSError:
        pass
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def render_table(results):
    """Таблица прогона: источник | статус | валидных | байт | ошибка."""
    rows = [("источник", "статус", "валидных", "байт", "ошибка")]
    for name in sorted(results):
        r = results[name]
        rows.append((name, r["status"],
                     "-" if r["count"] is None else str(r["count"]),
                     "-" if r["bytes"] is None else str(r["bytes"]),
                     r["error"] or ""))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip()
                     for row in rows)


def run(fetch=fetch_source, today=None, path=STATUS_FILE, out=print):
    """Прогон по всем источникам из PRESETS: обновляет JSON, печатает таблицу,
    возвращает код возврата (0/1). fetch подменяется в тестах - сети там нет.
    """
    today = today or utc_today()
    prev = load_state(path)
    results = {name: fetch(url) for name, url in PRESETS.items()}
    # состояние только по текущим источникам: выпавший из PRESETS не копит серию вечно
    state = {name: evaluate(prev.get(name), r, today) for name, r in results.items()}
    save_state(path, state)
    out(render_table(results))
    return exit_code(results, state)


if __name__ == "__main__":
    sys.exit(run())
