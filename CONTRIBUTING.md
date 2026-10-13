# Contributing

Issues and pull requests are welcome.

## Getting started

Python 3.9+ is required. Install the dependency and run the app:

```
pip install "mtproxy-checker @ git+https://github.com/alimghmi/mtproxychecker.git"
python tgproxy_gui.py
```

## Tests

Install the test dependencies and run the suite:

```
pip install mtproxy-checker pytest pytest-cov
python -m pytest -q
```

Coverage of `tgproxy_gui.py` must stay at 95% or higher. The threshold is set in `pytest.ini` (`--cov-fail-under=95`): a run below it fails. Make sure the suite is green before opening a pull request.

## Commit messages

The repository follows Conventional Commits: `<type>(<scope>): <summary>`.

- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
- The scope in parentheses is optional.
- The summary is in the imperative mood and has no trailing period.
- One commit is one logical change; keep commits small and focused.
- Messages may be written in Russian or English, both are fine.

The format is checked by the `commits` job in GitHub Actions (scope vocabulary and body conventions: see [AGENTS.md](AGENTS.md)); a message that does not follow the convention fails the build.

## Pull requests

- Run the tests before opening the PR.
- Keep the diff focused; do not mix refactoring with behavior changes.
- Update the README if the change is user-facing (new behavior, settings or limits).

## Language note

Code comments and some repository documents are in Russian. This is not a barrier to contributing: commit messages and pull requests in English are fine. If the Russian comments make it hard for you to work on the project, open an issue and we will help.

## Questions

Ask in GitHub issues: https://github.com/akenra/tgproxy/issues
