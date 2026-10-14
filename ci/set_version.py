#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Внедрение версии из тега в константу APP_VERSION файла tgproxy_gui.py.

Запуск: python ci/set_version.py v1.0.6 - шаг релизного workflow до pyinstaller.
Ожидается ровно одна замена; иначе - громкая ошибка и код 1: молчаливый пропуск
означал бы бинарь с версией из репозитория.
"""
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "tgproxy_gui.py"
# строка целиком: от начала до конца, значение в кавычках; (\r?) - чек-аут с CRLF
RX = re.compile(r'^(APP_VERSION = ")[^"]*(")(\r?)$', re.M)


def set_version(path, version):
    """Заменить значение APP_VERSION в файле path на version.
    Возврат: 0 - заменено; 1 - версия негодная, совпадений не ровно одно."""
    if not version or any(c in version for c in '"\n'):
        print(f"set_version: негодное значение версии: {version!r}")
        return 1
    # байты, а не read_text/write_text: скрипт не должен переводить переводы строк
    text = path.read_bytes().decode("utf-8")
    new, n = RX.subn(lambda m: m.group(1) + version + m.group(2) + m.group(3), text)
    if n != 1:
        print(f"set_version: в {path} совпадений APP_VERSION: {n}, "
              f"ожидалось ровно 1 - правку не вносил")
        return 1
    path.write_bytes(new.encode("utf-8"))
    print(f"set_version: APP_VERSION = {version}")
    return 0


def main():
    if len(sys.argv) != 2:
        print("использование: python ci/set_version.py vX.Y.Z")
        return 1
    tag = sys.argv[1]
    return set_version(APP, tag[1:] if tag.startswith("v") else tag)


if __name__ == "__main__":
    sys.exit(main())
