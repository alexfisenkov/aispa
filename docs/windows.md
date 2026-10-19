# Нативная Windows · alpha.5

Целевой контур — нативный Python 3.11+ и Git for Windows в локальном каталоге
Windows. WSL не заменяет эту проверку. Реализация и тесты включены в alpha.5;
успешное выполнение на Windows подтверждает только нативная строка CI для
конкретного commit. Полные модельные прогоны Codex, Claude Code и Cursor и
пользовательская приёмка остаются отдельными испытаниями.

Для alpha.5 нативная проверка уже подтверждена: [матрица трёх ОС](https://github.com/alexfisenkov/aispa/actions/runs/39257696524)
прошла для `58664f6`, включая Windows с Python 3.11 и 3.13. Последующие
изменения проходят ту же матрицу для своего commit.

## Python, Git и UTF-8

В PowerShell проверьте доступность Python и Git. Выберите один интерпретатор
для AISpa и команд проверки продукта; не смешивайте случайные `python`, `py`
и активированную venv. `py -3` выбирает установленный Python 3; если launcher
не установлен, замените его на `python` и проверьте версию.
Для clean-run выберите базовый Python вне исходного проекта: его прежняя
проектная `.venv` отклоняется, новая создаётся внутри чистого клона.

```powershell
Set-Location 'C:\Work\Учебный проект'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$Python = (py -3 -X utf8 -c "import sys; print(sys.executable)").Trim()
& $Python -X utf8 -c "import sys; assert sys.version_info >= (3, 11); print(sys.executable)"
git --version
$Project = (Get-Location).Path
$Aispa = (Resolve-Path '.agents\skills\aispa\tools\aispa.py').Path
& $Python -X utf8 $Aispa --version
```

Для Claude Code путь пакета — `.claude\skills\aispa`, для Cursor —
`.cursor\skills\aispa`. `-X utf8` включает UTF-8 для файловых операций Python;
явное указание кодировки в собственном коде остаётся предпочтительным.
[Документация Python о Windows UTF-8 mode](https://docs.python.org/3.11/using/windows.html#utf-8-mode).

Брифы и JSON сохраняйте как UTF-8. В Windows PowerShell 5.1 не полагайтесь на
кодировку перенаправления `>`: оно создаёт UTF-16LE.
[Документация Microsoft о кодировках PowerShell](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-7.5).
Например:

```powershell
[IO.File]::WriteAllText((Join-Path $Project 'brief.md'), 'Сделать каталог с поиском.', [Text.UTF8Encoding]::new($false))
```

## Сохранённый runtime и команды

После обычного `init` используйте копию пакета текущего прогона. Пример выбора
пути для уже созданного прогона:

```powershell
$State = (& $Python -X utf8 $Aispa --root $Project status) | ConvertFrom-Json
$Aispa = Join-Path $Project ".aispa\$($State.runDirectory)\runtime\tools\aispa.py"
& $Python -X utf8 $Aispa --root $Project status
```

Значение `--runtime` при создании берётся из фактической агентской среды:
`codex`, `claude-code` или `cursor`. Оно не выбирает ОС и не заменяет Python.
Команды передаются списком аргументов после `--`; оболочка не добавляется
неявно. Для скрипта PowerShell укажите `powershell.exe` или `pwsh.exe` явно.
Предпочитайте прямую команду `python.exe`, если оболочка не нужна.

Кавычки вокруг путей с пробелами и кириллицей обязательны в примерах
PowerShell. Не вставляйте bash-продолжения `\`, `export` и `.venv/bin/python`.
Для многострочных команд удобнее массив аргументов PowerShell, чем переносы
с обратной кавычкой.

## Чистый клон и зависимости

`clean-prepare` требует чистый сохранённый HEAD и `.aispa/` в `.gitignore`.
Рабочие файлы, локальная `.venv` и ignored-зависимости исходного проекта в клон
не копируются. После подготовки устанавливайте зависимости внутри клона.
Ниже пример для проекта с committed `requirements.lock` и тестами unittest;
сначала добавьте `.venv/` в `.gitignore` проекта и сохраните это изменение.

```powershell
& $Python -X utf8 $Aispa --root $Project clean-prepare
& $Python -X utf8 $Aispa --root $Project clean-verify -- $Python -m venv .venv
& $Python -X utf8 $Aispa --root $Project clean-verify -- '.venv\Scripts\python.exe' -m pip install --require-hashes -r requirements.lock
& $Python -X utf8 $Aispa --root $Project clean-verify -- '.venv\Scripts\python.exe' -m unittest discover -s tests -v
```

После каждой команды проверьте `$LASTEXITCODE`: ненулевой код требует разбора
перед продолжением. Установка зависимости не заменяет итоговую проверку
продукта. Без сети pip нужен доступ к заранее сохранённым wheel и
`--no-index`; автоматический тест AISpa действительно устанавливает такой
wheel с проверкой хеша, а затем импортирует его из venv.

На Windows интерпретатор venv находится в `Scripts\python.exe`. Активировать
venv не требуется: полный или clone-relative путь выбирает интерпретатор
напрямую. Создавайте окружение тем же `$Python`; перенос готовой venv с другой
машины не является установкой зависимостей.
[Документация Python venv](https://docs.python.org/3.11/library/venv.html).

Окружение clean-run очищается от пользовательских токенов, SSH-агента,
`PYTHONPATH`, `NODE_PATH` и пользовательских конфигураций. Windows сохраняет
необходимый `SystemRoot`; `HOME`, `USERPROFILE` и временные каталоги указывают
на служебные каталоги клона. Не рассчитывайте на активированную исходную venv
или произвольные программы из пользовательского `PATH`.

При timeout и после завершения родителя Windows Job Object останавливает
оставшиеся дочерние процессы. POSIX использует process group. Это управление
жизненным циклом, а не sandbox: процесс всё ещё имеет права текущего
пользователя и доступ к сети.
[Документация Microsoft о Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

## Проверка AISpa

Из корня исходников AISpa:

```powershell
python -X utf8 -B -m unittest discover -s tests -v
node --test tests/dashboard.test.cjs
```

Либо используйте `py -3 -X utf8 -B -m unittest discover -s tests -v`.
Node 18+ нужен только для JS-тестов интерфейса. Для полного Python-набора
нужны права создавать символические ссылки: Developer Mode либо соответствующая
привилегия процесса. Тесты защиты путей создают реальные ссылки и не отключаются
на Windows. Обычному запуску AISpa создание таких тестовых ссылок не требуется.
[Документация Python os.symlink](https://docs.python.org/3.11/library/os.html#os.symlink).

В Windows выполняются полный CLI-путь до finish, blind-проверки, clean-run,
реальная offline-установка pip, Unicode-пути и вывод, таймаут и очистка
дочерних процессов. Только проверки механизма другой ОС могут иметь явный
skip; PASS набора с отключённым clean-run не считается Windows-приёмкой.

Tracked symlinks и submodules в clean-clone отклоняются. Сетевые и UNC-каталоги,
особые файловые системы, сторонние политики запуска и запуск внутри чужого
ограничивающего Job Object не считаются подтверждёнными одной строкой CI.
Если runtime не может обеспечить остановку дерева процессов, запуск должен
завершиться ошибкой, а не получить PASS с ослабленными условиями.

Обновление установленного навыка запускайте из корня проекта или другой папки
вне самого пакета. Windows удерживает рабочий каталог процесса: при cwd внутри
пакета updater откажется заменять его и сообщит cwd_inside_package.

Текстовые LF/CRLF сравниваются с точным Git blob после единственного разрешённого
преобразования CRLF → LF; binary/-text и реальные изменения не скрываются.
Git submodules, tracked symlinks, внешние фильтры, working-tree-encoding, ident
и SHA-256 repositories в этой альфе отклоняются явно.
