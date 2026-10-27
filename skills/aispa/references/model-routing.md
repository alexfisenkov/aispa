# Маршруты моделей

Единственный справочник с ID моделей и усилиями для ролей AISpa. Остальные
тексты скилла ссылаются сюда. В движке ту же таблицу держит
`tools/aispa_core/model_routes.py`; тест сверяет их построчно, и любое
расхождение — ошибка.

Таблица действует для новых прогонов (флаг политики `modelRoutes`) и строго
применяется в средах Claude Code и Codex. На каждый класс и среду утверждены
одна модель и одно усилие. Замен нет: если утверждённой модели в среде нет,
работа не уходит молча на другую модель — это блокер с вариантами для
пользователя.

## Таблица

| Класс | Роли | Claude Code | Codex | Агент Claude Code |
|---|---|---|---|---|
| `coordinator` (только совет) | `coordinator` — главный чат | `claude-opus-5-5` · high на брифе и плане, дальше medium | `gpt-6.1-sol` · high на брифе и плане, дальше medium | — |
| `judge` | `critic1`, `critic2`, `acceptor`, `blind`, `integration-reviewer`, `branch-reviewer`, `brief-analyst`, `brief-reviewer`, `coverage`, `idea`, `planner`, `recovery-reviewer` | `claude-opus-5-5` · high | `gpt-6.1-sol` · high | `aispa-judge` |
| `sensitive_worker` | `worker` задачи с ключами, деньгами, доступами или личными данными | `claude-opus-5-5` · high | `gpt-6.1-sol` · high | `aispa-sensitive-worker` |
| `worker` | `worker` обычной задачи | `claude-sonnet-5-5` · medium | `gpt-6.1-terra` · medium | `aispa-worker` |
| `mechanical_scripted` | `worker` механической задачи, у которой план объявил скриптовую проверку | `claude-haiku-5-5` · low | `gpt-6-luna` · low | `aispa-mechanic-scripted` |
| `mechanical` | `worker` механической задачи без скриптовой проверки | `claude-sonnet-5-5` · low | `gpt-6.1-terra` · low | `aispa-mechanic` |
| `research` | `discovery`, `source-reader`, `knowledge-writer` | `claude-opus-5-5` · medium | `gpt-6.1-terra` · high | `aispa-researcher` |

Класс исполнителя задачи определяет движок, а не объявление в плане:
чувствительная задача — `sensitive_worker`; механическая работа со скриптовой
проверкой из плана — `mechanical_scripted`, без неё — `mechanical`; остальное —
`worker`.

## Главный чат

Модель главного чата движок только советует и ни в какой модели не отказывает.
Совет: в Claude Code — `claude-opus-5-5`, в Codex — `gpt-6.1-sol`; усилие high на
брифе и плане, дальше medium. Fable или Astra — только ваш осознанный выбор.
Ответ `init` содержит этот совет в поле `coordinatorAdvice`.

## Субагенты

- Субагентам не дают Fable, Astra, `inherit`, пустую модель и усилие выше high.
- Модель и усилие берутся из таблицы для класса роли в своей среде.
- В Claude Code для каждого класса есть свой файл агента с теми же моделью и
  усилием; координатор вызывает агента по имени из таблицы.

## Алиасы Claude Code

Claude Code принимает только эти алиасы, без разбора по префиксу:

- `opus` → `claude-opus-5-5`
- `sonnet` → `claude-sonnet-5-5`
- `haiku` → `claude-haiku-5-5`

## Другие среды

В Gemini, Cursor, OpenClaw и Antigravity таблица не применяется строго: совет
модели остаётся прежним, а запреты для субагентов действуют везде.
