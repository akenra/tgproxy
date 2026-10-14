"""Тесты ci/check_commit_messages.py: разбор диапазона и fallback при недоступном before.

Репозиторий-фикстура и сам чекер запускаются через subprocess в tmp_path:
тесты детерминированы, не ходят в сеть и не зависят от состояния репозитория
проекта.
"""
import os
import pathlib
import subprocess
import sys

import pytest

CHECKER = pathlib.Path(__file__).resolve().parent / "ci" / "check_commit_messages.py"
ZERO = "0" * 40
# 40 hex-символов, заведомо отсутствующих в тестовом репозитории: имитация sha,
# оставшегося за пределами клона после force-push.
ABSENT = "deadbeef" * 5
BAD = "правка без типа"


def git(repo, *args):
    p = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=repo,
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, "git %s: %s" % (" ".join(args), p.stderr)
    return p.stdout


@pytest.fixture()
def repo(tmp_path):
    """Репозиторий из четырёх коммитов: нарушитель BAD - второй (HEAD~2).

    HEAD~3 feat, HEAD~2 BAD, HEAD~1 fix, HEAD docs.
    """
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "t")
    for msg in ("feat: первый коммит", BAD, "fix: третий коммит",
                "docs: четвёртый коммит"):
        git(r, "commit", "-q", "--allow-empty", "-m", msg)
    return r


def check(repo, *args):
    """Прогон чекера так же, как в CI: отдельным процессом в каталоге репо."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, str(CHECKER), *args], cwd=repo, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout


def test_range_mid_violator_returns_1(repo):
    """(а) валидный диапазон: нарушитель в середине - код 1 и он в выводе."""
    rc, out = check(repo, "HEAD~3..HEAD")
    assert rc == 1
    assert BAD in out


def test_range_all_conventional_returns_0(repo):
    """(а) валидный диапазон без нарушителей - код 0."""
    rc, out = check(repo, "HEAD~2..HEAD")
    assert rc == 0, out
    assert "Формат сообщений: ок" in out


def test_absent_before_good_tip_returns_0_with_note(repo):
    """(б) недоступный before, кончик conventional: код 0 и пометка."""
    rc, out = check(repo, ABSENT + "..HEAD")
    assert rc == 0, out
    assert "before недоступен в клоне" in out
    assert "проверен только последний коммит" in out
    assert "не удалось прочитать диапазон" not in out


def test_absent_before_bad_tip_returns_1(repo):
    """(в) недоступный before, кончик-нарушитель: код 1 и он в выводе."""
    rc, out = check(repo, ABSENT + "..HEAD~2")
    assert rc == 1
    assert BAD in out


def test_zero_before_checks_tip_only(repo):
    """(г) ZERO-before: проверяется только кончик пуша."""
    rc, out = check(repo, ZERO + "..HEAD")
    assert rc == 0, out
    assert BAD not in out  # нарушитель старше кончика не притягивается
    rc, out = check(repo, ZERO + "..HEAD~2")
    assert rc == 1
    assert BAD in out


def test_single_rev_argument_unchanged(repo):
    """Аргумент без .. (форма HEAD^!) через fallback не проходит."""
    rc, out = check(repo, "HEAD^!")
    assert rc == 0, out
    assert "before недоступен" not in out


def test_invalid_to_returns_2(repo):
    """(д) невалидный to: прежняя ошибка диапазона, код 2."""
    rc, out = check(repo, "HEAD.." + ABSENT)
    assert rc == 2
    assert "не удалось прочитать диапазон" in out
    assert "before недоступен" not in out  # before (HEAD) валиден: fallback не нужен


def test_absent_before_and_invalid_to_returns_2(repo):
    """Оба конца недоступны: fallback не маскирует невалидный to, код 2."""
    rc, out = check(repo, ABSENT + ".." + ABSENT)
    assert rc == 2
    assert "не удалось прочитать диапазон" in out
