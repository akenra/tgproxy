#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка формата сообщений коммитов: Conventional Commits (канон в AGENTS.md).

Запуск:  python ci/check_commit_messages.py <range>
где <range> - git-диапазон, например base..head, before..sha или HEAD~5..HEAD.
Нулевой before (пуш нового бранча) или пустой аргумент: проверяется только
последний коммит. Недоступный before (force-push) - тоже только кончик пуша,
но с пометкой. Merge-коммиты не проверяются.
"""
import re
import subprocess
import sys

TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build",
         "ci", "chore", "revert")
RX = re.compile(r"^(%s)(\([a-z0-9-]+\))?!?: \S" % "|".join(TYPES))
ZERO = "0" * 40
NOTE_BEFORE_MISSING = ("before недоступен в клоне (вероятно force-push): "
                       "проверен только последний коммит")


def resolves(rev):
    """Резолвится ли ревизия в коммит локально: после force-push before исчезает."""
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                          capture_output=True).returncode == 0


def main():
    rng = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not rng:
        rng = "HEAD^!"
    else:
        frm, sep, to = rng.partition("..")
        if frm == ZERO:
            rng = to + "^!"
        elif sep and frm and not resolves(frm):
            # before не добрался до клона: проверяем кончик пуша.
            # Потолок компромисса: переписанные коммиты старше кончика не
            # проверяются; расширять - при появлении реального кейса.
            print(NOTE_BEFORE_MISSING)
            rng = to + "^!"

    out = subprocess.run(["git", "log", "--no-merges", "--format=%h%x09%s", rng],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        print("не удалось прочитать диапазон:", rng)
        print(out.stderr.strip())
        return 2

    bad = []
    for line in out.stdout.splitlines():
        sha, subject = line.split("\t", 1)
        if not RX.match(subject):
            bad.append((sha, subject))
    if bad:
        print("Сообщения вне Conventional Commits (канон: AGENTS.md, COMMIT MESSAGES):")
        for sha, subject in bad:
            print("  %s  %s" % (sha, subject))
        return 1
    print("Формат сообщений: ок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
