# Первая установка AISpa из опубликованного Release

Текущий исходный код — `0.1.0-alpha.10`; выпуск этой версии ещё не подтверждён.
Установщик берут только из **опубликованного** Release
[`alexfisenkov/aispa`](https://github.com/alexfisenkov/aispa/releases), где есть
три ресурса: `install.py`, `aispa.zip`, `aispa-update.json`. До появления всех
трёх новый выпуск нельзя считать доступным для установки.

Нужны Python 3.11+ и Git. Скачайте `install.py` из выбранного Release в рабочую
папку и сохраните ссылку на этот выпуск. `preview` читает опубликованный tag,
проверяет точный Git commit, manifest, ZIP, хеши и состав файлов и создаёт
кандидата для просмотра. Он не создаёт папку навыка.

Пример проектной установки Codex из папки, где лежит скачанный `install.py`:

```text
python3 install.py preview --runtime codex --scope project --project-root <абсолютный-корень-проекта>
```

На Windows используйте доступный Python 3.11+: `python -X utf8 install.py`
или `py -3 -X utf8 install.py` с теми же аргументами. Для Claude Code укажите
`--runtime claude-code`, для Cursor — `--runtime cursor`. Проектная установка
выбирает `.agents/skills/aispa/`, `.claude/skills/aispa/` или
`.cursor/skills/aispa/` соответственно. Глобальная установка использует
`--scope global` без `--project-root`; она не выполняется автоматически.
Вместо runtime/scope можно указать отдельный свободный абсолютный каталог
через `--destination`.

После `preview` проверьте в квитанции `candidateId`, `destination`, `tag`,
`version`, `commit`, `releaseUrl`, `archiveSha256` и `candidateDigest`. Когда
выбран именно этот кандидат и путь свободен:

```text
python3 install.py apply --candidate <candidateId> --confirm
```

`--confirm` означает уже принятое решение об установке. Apply повторно
проверяет файлы кандидата и создаёт **новую** папку назначения; существующий
пакет не перезаписывается. Если каталог появился после preview или проверка
разошлась, операция останавливается. Частично созданный каталог при ошибке
сохраняется для осмотра, а не удаляется вслепую. Кандидат и квитанции остаются
в `~/.aispa-installs/`, если не выбран иной `--cache-dir`.

После установки откройте `SKILL.md` установленного пакета. Его первое действие
при явном вызове — `update-check`; обновление действующего пакета проходит
отдельный цикл prepare → просмотр → apply из [регламента обновлений](updates.md).
Начатый прогон продолжает работать своей закреплённой копией runtime.

Доверие к установке основано на фиксированном GitHub publisher, HTTPS,
опубликованном tag, проверенном commit и хешах ресурсов. Это не независимая
цифровая подпись. Копирование исходников из ветки main или одного ZIP без
сверки Release не заменяет этот маршрут. Проверка в коде — `tools/install.py`,
сборка трёх ресурсов — `tools/package_update.py` и
`.github/workflows/release.yml`.
