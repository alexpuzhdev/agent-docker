# agent-docker

Единственный репозиторий: `agent-docker`. Базовая ветка для работы над гостевыми VM: `v2-kve`.

- Настройка первого гостя: `make` или `make vm-setup`.
- Сценарии Docker Compose для гостевой VM хранятся в `v2/compose.yaml`.
- Проверка shell-сценариев: `bash -n v2/manage.sh v2/start-vm.sh v2/guest.sh v2/prepare-base.sh v2/prepare-guest.sh v2/serial-profile.sh`.
- Проверка JS: `node --check` для каждого изменённого файла `.cjs`.
- Проверка Git-шлюза: `node --test v2/tests/test_git_gateway.cjs`.
- Проверка настройки Git через Make: `bash v2/tests/test_vm_setup_git.sh`.
- Проверка статуса Git-шлюза: `bash v2/tests/test_vm_git_status.sh`.
- Проверка гостевой консоли: `python3 -m unittest discover -s v2/tests -v`.
- Этап сквозного теста: `make vm-setup VM=vm-01`, проверка результата установки, затем `make vm-stop VM=vm-01` и повторный запуск с тем же диском.
- Шлюз принимает HTTP и HTTPS только во внутренней сети Compose. Порты 80 и 443 на хосте не публикуются.
- Инструкция для пользователя находится в `GUIDE.md`.
