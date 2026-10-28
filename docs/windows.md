# Нативная Windows

Целевой контур — нативная Windows; WSL для AISpa не нужен и проверку на
Windows не заменяет.

## Что нужно

- нативный Python 3.11 или новее;
- Git for Windows;
- Claude Code или Codex, установленные на самой Windows, и папка проекта на
  локальном диске Windows.

Установка и обновление — как на других системах: [установка](install.md),
[обновление](updates.md). Запускайте Python с включённым UTF-8:
`py -3 -X utf8` или `python -X utf8`. Брифы и файлы с данными сохраняйте как
UTF-8; в Windows PowerShell 5.1 перенаправление `>` создаёт UTF-16, поэтому
пишите файлы через агента или задавайте кодировку явно.

Проверить, что нужное на месте:

```powershell
py -3 -X utf8 -c "import sys; assert sys.version_info >= (3, 11); print(sys.version)"
git --version
```

## Чем проверяется

Одна ручная матрица CI из файла
[`.github/workflows/checks.yml`](../.github/workflows/checks.yml): полный набор
тестов на трёх системах (Ubuntu, macOS, Windows) с Python 3.11 и 3.13.
Запускается вручную один раз на итоговой версии, на push и pull request не
идёт. Зелёная строка Windows для точного commit — единственное подтверждение
поддержки Windows для этого commit; проверка на другой системе её не заменяет.
