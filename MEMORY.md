# MEMORY

## Назначение

Краткая память по workspace `C:\pro\vex-assistant`.

## Что это за workspace

- Этот workspace относится к проекту `Среда`.
- Старый проект `Ассистент` сюда не смешивать.

## Текущая структура

- код `Среды`: `C:\pro\vex-assistant\sreda`
- приватные фичи: `C:\pro\vex-assistant\sreda-private-features`
- документация: `C:\pro\vex-assistant\sreda\docs`
- публичная документация: `C:\pro\vex-assistant\sreda\docs\public`

## Что важно помнить

- В публичный репозиторий `sreda` можно класть только `docs/public`.
- `docs/internal` не должен попадать в публичный git.
- Секреты `Среды` держать в `sreda/.secrets/`.
- В коде и docs `Среды` использовать `.sreda/...`, а не legacy `.openclaw/...`.
- Новые фичи сначала через specs/docs, потом через код.

## Что читать в новой сессии

- `AGENTS.md`
- `MEMORY.md`
- `ERRORS.md`
