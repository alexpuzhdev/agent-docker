# agent-docker

Единственный репозиторий: `agent-docker`. Базовая ветка для работы над гостевыми VM: `v2-kve`.

- Настройка первого гостя: `make` или `make vm-setup`.
- Сценарии Docker Compose для гостевой VM хранятся в `v2/compose.yaml`.
- Проверка shell-сценариев: `bash -n v2/manage.sh v2/start-vm.sh v2/guest.sh v2/prepare-base.sh v2/prepare-guest.sh v2/serial-profile.sh v2/guest-dev-tools.sh`.
- Проверка JS: `node --check` для каждого изменённого файла `.cjs`.
- Проверка Git-шлюза: `node --test v2/tests/test_git_gateway.cjs` и `python3 -m unittest v2/tests/test_git_mirror.py`.
- Проверка журнала SNI: `node --test v2/tests/test_sni_log.cjs`.
- Проверка настройки Git через Make: `bash v2/tests/test_vm_setup_git.sh`.
- Проверка статуса Git-шлюза: `bash v2/tests/test_vm_git_status.sh`.
- Проверка журналов, профиля и удаления гостя: `bash v2/tests/test_vm_manage_targets.sh`.
- Сквозная проверка GitLab по HTTPS на подставном сервере: `bash v2/tests/test_gitlab_https.sh`. Нужны `python3`, `git`, `openssl` и `git-http-backend`; сеть не требуется, стенд работает на loopback. Первым аргументом можно передать путь к проверяемому `git-mirror.py` — это нужно для мутационных прогонов на копии.
- Проверка терминального приложения: `gofmt -l cli`, `go vet ./cli` и `go test ./cli`.
- Проверка подготовки гостя и поиска устройства cloud-init: `bash v2/tests/test_prepare_guest.sh`.
- Проверка гостевой консоли: `python3 -m unittest discover -s v2/tests -v`.
- Этап сквозного теста: `make vm-setup VM=vm-01`, проверка результата установки, затем `make vm-stop VM=vm-01` и повторный запуск с тем же диском.
- Шлюз принимает HTTP и HTTPS только во внутренней сети Compose. Порты 80 и 443 на хосте не публикуются.
- Исходники терминального приложения `agd` лежат в `cli`. Оно не повторяет логику `v2/manage.sh`, а вызывает его цели и не работает с токенами.
- Инструменты разработки в госте: `bash v2/guest-dev-tools.sh --check` сверяет набор и версии,
  минимум для Go берётся из директивы `go` в `go.mod`. `--install` доставляет недостающее из
  разрешённых источников (`/etc/apt/agent-docker.list`: `deb.debian.org` и `downloads.claude.ai`
  через upstream-прокси) и требует root, поэтому запускается на отдельном стенде, а не в живом
  госте. При полном наборе apt не вызывается, так что повторный запуск безопасен.
- Проверки Go в госте запускайте с `GOTOOLCHAIN=local`: прямого выхода в сеть нет, и расхождение
  с директивой `go` должно давать отказ, а не загрузку тулчейна.
- Для `go test -race` нужен gcc: без компилятора C детектор гонок не собирается.
- Проверка доставки гостевых инструментов: `bash v2/tests/test_guest_dev_tools.sh`. Настоящие apt,
  сеть и root не нужны — набор инструментов подставной.
- Docker, `docker compose`, `qemu-system-x86_64`, `qemu-img` и `genisoimage` нужны только на хосте
  Docker: остальные проверки из этого списка обходятся подставными командами в `PATH`.
- Инструкция для пользователя находится в `GUIDE.md`.
