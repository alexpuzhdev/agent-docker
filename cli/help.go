package main

import "errors"

// errFlagRoot — единственное сообщение о неверном общем флаге.
var errFlagRoot = errors.New("--root требует путь к каталогу agent-docker")

// version печатается по agd version и попадает в заголовок меню.
const version = "0.1.0"

// helpText — единственная справка CLI: она же показывается при ошибке разбора.
const helpText = `agd — управление изолированными окружениями agent-docker.

Использование:
  agd                          полноэкранное меню (нужен терминал)
  agd <команда> [флаги] <VM>

Окружения:
  ls                           список гостей: состояние, ресурсы, прокси, Git
  status <VM>                  подробное состояние одного гостя
  new <VM>                     создать и подготовить гостя
    --cpu N --ram N --disk N     бюджет при первом создании
    --repo R --provider P        назначить Git-шлюз сразу при создании
    --url U --write BRANCH
    --sni-log                    включить журнал SNI на шлюзе
  setup <VM>                   повторная подготовка после обновления образа
  up [VM]                      запустить гостя, без имени — все настроенные
  stop <VM>                    штатно выключить гостя и остановить шлюз
  rm <VM>                      удалить гостя
    --keep-disk                  сохранить диск (по умолчанию)
    --delete-disk                удалить диск вместе с проектами
    --yes                        не спрашивать подтверждение

Работа агента:
  console <VM> [--project P]   оболочка гостя
  claude <VM> [--project P] [--resume ID]
  auth <VM>                    вход Claude Code внутри гостя
  logout <VM>                  выход Claude Code

Шлюзы:
  proxy <VM>                   задать upstream-прокси гостя
  git <VM> --repo owner/name [--write BRANCH]
  git <VM> --provider gitlab --url https://gitlab.example.com --repo group/project
  git status <VM>              показать назначение шлюза без токена

Диагностика:
  logs <VM> [--service vm|proxy] [--tail N|all] [--follow]
  profile <VM>                 идентификаторы запущенного гостя

Общие флаги:
  --root PATH                  каталог agent-docker (иначе AGENT_DOCKER_ROOT
                               либо поиск от каталога agd и текущего каталога)

Токены и пароли вводятся в запросе и остаются на хосте: CLI их не печатает,
не принимает в аргументах и не сохраняет в истории оболочки.
`
