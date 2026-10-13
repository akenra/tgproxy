#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка формата сообщений коммитов: Conventional Commits (канон в AGENTS.md).

Запуск:  python ci/check_commit_messages.py <range>
где <range> - git-диапазон, например base..head, before..sha или HEAD~5..HEAD.
Нулевой before (пуш нового бранча) или пустой аргумент: проверяется только
последний коммит. Merge-коммиты не проверяются.
"""
import re
import subprocess
import sys

TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build",
         "ci", "chore", "revert")
RX = re.compile(r"^(%s)(\([a-z0-9-]+\))?!?: \S" % "|".join(TYPES))
ZERO = "0" * 40


def main():
    rng = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not rng:
        rng = "HEAD^!"
    else:
        frm, _, to = rng.partition("..")
        if frm == ZERO:
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
