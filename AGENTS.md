# agent-docker

Единственный репозиторий: `agent-docker`. Базовая ветка: `main`.

- Запуск контейнеров: `make up`.
- Установка CLI: `make init-claude`, `make init-codex` или `make init`.
- Проверка конфигурации: `docker compose config --quiet` после заполнения `.env`.
- Проверка сценария запуска: `sh -n tinyproxy-start`, затем установка выбранного CLI в тестовом проекте Compose и проверка его версии через `docker compose exec -T agents`.
- Tinyproxy слушает порт `8888` во внутренней сети Docker. Порт остаётся внутри Docker.
- Подробная инструкция для пользователя находится в `GUIDE.md`.
