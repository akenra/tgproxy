# TG Proxy Checker

Кроссплатформенное GUI-приложение: качает свежий список публичных MTProto-прокси,
проверяет каждый полным хендшейком (не просто «порт открыт», а реальный ответ Telegram),
и кладёт живых в буфер обмена одним списком для вставки в Telegram.

## Запуск

Нужен Python 3.9+:

```
pip install mtproxy-checker
python tgproxy_gui.py
```

Или одной командой:

```
pip install mtproxy-checker && python tgproxy_gui.py
```

## Как пользоваться

1. **Загрузить** — качает актуальный список (135+ прокси из поддерживаемого источника).
2. **Проверить** — прогоняет все адреса через полный MTProto-хендшейк в 32 потока,
   занимает около минуты, мёртвые отсеиваются по факту, а не по порту.
3. **Включить** — копирует живых в буфер одним списком и открывает Telegram.
   В Telegram: Настройки → Данные и память → Прокси → Добавить прокси →
   вставить (Ctrl+V), Telegram распарсит весь список за раз.
   Двойной клик по строке открывает одиночную t.me-ссылку конкретного прокси.

## Тесты и правило покрытия

Покрытие `tgproxy_gui.py` должно быть **не ниже 95%**. Правило вшито в `pytest.ini`
(`--cov-fail-under=95`): любой прогон тестов без нужного покрытия падает.

```
pip install mtproxy-checker pytest pytest-cov
python -m pytest -q
```

Сборку делайте только после зелёных тестов: это и есть gate.

## Сборка в один файл (для раздачи людям без Python)

Windows (.exe), на машине с Windows:

```
pip install pyinstaller
python -m pytest -q          # gate: сборка только после зелёных тестов
pyinstaller --onefile --windowed --name tgproxy_gui tgproxy_gui.py
```

Linux/macOS:

```
python -m pytest -q          # gate
pyinstaller --onefile --name tgproxy_gui tgproxy_gui.py
```

Готовый бинарь появится в `dist/`. Собирайте на той ОС, под которую нужен файл:
PyInstaller не кросс-компилирует. В `.github/workflows/` можно поставить матрицу
сборки для всех трёх ОС одним пушем — но это опционально.

## Что внутри

Один файл `tgproxy_gui.py`, stdlib (`tkinter`, `urllib`, `concurrent.futures`)
плюс единственная зависимость `mtproxy-checker`. Без настройки, без конфигов,
без состояния между запусками.
