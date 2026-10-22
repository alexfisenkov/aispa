# Установка AISpa из опубликованного Release

Опубликован [AISpa 0.3.0](https://github.com/alexfisenkov/aispa/releases/tag/v0.3.0):
Codex, Claude Code, Cursor, OpenClaw, Gemini CLI и Antigravity app/IDE.
Выпуск на Mac разрешён владельцем до завершения Windows CI;
[подтверждённые проверки](validation-0.3.0.md).

Всегда берите `install.py`, `aispa.zip` и `aispa-update.json` из одного выбранного
Release. Установщик из старого выпуска может не знать новые значения runtime.
Нужны Python 3.11+ и Git; на Windows используйте `python -X utf8` или `py -3 -X utf8`.
Скачивание и общие шаги — [README](../README.md#установка).

## Среда и область установки

| runtime | project | global | Доступность |
|---|---|---|---|
| codex | `.agents/skills/aispa` | `~/.codex/skills/aispa` | опубликовано |
| claude-code | `.claude/skills/aispa` | `~/.claude/skills/aispa` | опубликовано |
| cursor | `.cursor/skills/aispa` | `~/.cursor/skills/aispa` | опубликовано |
| openclaw | `.agents/skills/aispa` | `~/.openclaw/skills/aispa` | опубликовано в 0.3.0 |
| gemini | `.gemini/skills/aispa` | `~/.gemini/skills/aispa` | опубликовано в 0.3.0 |
| antigravity | `.agents/skills/aispa` | `~/.gemini/config/skills/aispa` | опубликовано · app/IDE |

Project-пути отсчитываются от `--project-root`. Для global этот аргумент
не нужен. Общая Codex-копия `~/.agents/skills/aispa` выбирается через
`--destination`; существующую папку обновляют, а не устанавливают поверх неё.
Приоритеты дублирующих копий и Antigravity CLI — [адаптеры](../skills/aispa/references/native-agents.md).

## Preview и apply

Для опубликованных сред, из папки со скачанным установщиком:

```text
python3 install.py preview --runtime claude-code --scope project --project-root <абсолютный-корень-проекта>
```

Установщик выпуска 0.3.0 принимает:

```text
python3 install.py preview --runtime openclaw --scope global
python3 install.py preview --runtime gemini --scope global
python3 install.py preview --runtime antigravity --scope global
```

Это альтернативы: выберите нужную среду. Пока подходящего выпуска нет,
новый установщик отклоняет пакет, в котором выбранный runtime не объявлен.
Номер версии и новый путь сами по себе не доказывают поддержку.
Вместо runtime/scope можно задать свободный абсолютный `--destination`;
он не выбирает и не доказывает совместимость среды.

Preview проверяет опубликованный tag/commit, manifest, SHA-256 ZIP, состав,
размеры и права файлов, сохраняет кандидата в `~/.aispa-installs/` либо в
выбранном `--cache-dir`. Папка назначения ещё не создаётся.
Проверьте `candidateId`, `destination`, `runtime`, `version`, `prerelease`,
`commit`, `releaseUrl`, `archiveSha256`, `candidateDigest`. После решения об установке:

```text
python3 install.py apply --candidate <candidateId> --confirm
```

При нестандартном кэше передайте тот же `--cache-dir` в apply. Подтверждение
не заменяет выбор пользователя. Apply повторно проверяет кандидата и
поддержку runtime, не перезаписывает существующую папку, отказывается при
конфликте/изменении кандидата. Черновик, main или tag без Release не подходят.

## После установки

Прочитайте установленный SKILL.md, откройте новую сессию агента и проверьте
каталог навыков. При явном вызове первым действием выполняется `update-check`.
Обновление существующего пакета и откат — [updates.md](updates.md).
Активный прогон продолжает работать собственным закреплённым runtime.

Проверенная доставка 0.2.0 описана в [validation-0.2.0.md](validation-0.2.0.md).
Адресные тесты выпуска — [validation-agent-runtimes.md](validation-agent-runtimes.md).
Они не подтверждают полное модельное исполнение во всех приложениях.

## Выбор опубликованного выпуска с 0.3.4

Без `--tag` установщик сверяет ограниченный список Releases с latest и один
раз повторяет пустую первую страницу. Все запросы обнаружения используют
один общий тайм-аут. Непустой список с ошибкой latest не считается успешной
проверкой; существующие файлы не изменяются.

Для конкретной опубликованной версии укажите, например, `--tag v0.3.4` в
команде `preview`. Тогда проверяется отдельный адрес Release этого тега,
а список не запрашивается. Обычный Git tag без опубликованного Release
не подходит. Проверки происхождения, хешей и подтверждение `apply` сохраняются.
