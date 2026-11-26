#!/bin/bash
# Сборка и пользовательская установка терминального приложения atrice.
#
#   bash cli/install.sh                  собрать и положить в ~/.local/bin
#   bash cli/install.sh --bind           то же и привязать каталог проекта
#   bash cli/install.sh --prefix DIR     другой каталог для бинарника
#                                        (относительный — от каталога вызова)
#   bash cli/install.sh --name agd       поставить под прежним именем
#
# Права root не нужны: по умолчанию бинарник кладётся в каталог пользователя.
# Имя бинарника ни на что не влияет — приложение не смотрит на argv[0], —
# поэтому ранее собранный agd продолжает работать рядом.
#
# Файл привязки к каталогу проекта пишется только по --bind. Без него уже
# настроенная привязка остаётся как была: установщик не переписывает
# пользовательские настройки без явного запроса.
set -euo pipefail

source_root=$(cd "$(dirname "$0")/.." && pwd -P)
prefix=${XDG_BIN_HOME:-$HOME/.local/bin}
config=${XDG_CONFIG_HOME:-$HOME/.config}/agent-docker/root
name=atrice
bind=''
usage() { printf 'Использование: %s [--bind] [--prefix DIR] [--name NAME]\n' "${0##*/}" >&2; exit 2; }
while test $# -gt 0; do
    case $1 in
        --bind) bind=1 ;;
        --prefix) shift; test $# -gt 0 || usage; prefix=$1 ;;
        --prefix=*) prefix=${1#--prefix=} ;;
        --name) shift; test $# -gt 0 || usage; name=$1 ;;
        --name=*) name=${1#--name=} ;;
        *) usage ;;
    esac
    shift
done
case $name in
    ''|*/*) echo 'Имя бинарника задаётся без косой черты.' >&2; exit 2 ;;
esac
test -n "$prefix" || { echo 'Каталог установки не может быть пустым.' >&2; exit 2; }

command -v go >/dev/null ||
    { echo 'Нужен Go. Проверить набор: bash v2/guest-dev-tools.sh --check' >&2; exit 1; }

mkdir -p "$prefix"
# Относительный путь разрешается от каталога вызова, а не от корня проекта.
# Сборка идёт с переходом в корень, поэтому `go build -o` с относительным
# путём писал бинарник в корень проекта, тогда как каталог создавался рядом с
# каталогом вызова: установщик сообщал об одном пути, а файл появлялся в
# другом. Разрешается уже созданный каталог, поэтому pwd не может промахнуться.
prefix=$(cd -- "$prefix" && pwd -P) ||
    { printf 'Не удалось разрешить каталог установки %s.\n' "$prefix" >&2; exit 1; }
# GOTOOLCHAIN=local: прямого выхода в сеть у гостя нет, и расхождение с
# директивой go в go.mod должно давать отказ, а не загрузку тулчейна.
(cd "$source_root" && GOTOOLCHAIN=local go build -o "$prefix/$name" ./cli)
printf 'Установлено: %s\n' "$prefix/$name"

case ":${PATH-}:" in
    *":$prefix:"*) ;;
    *) printf 'Каталог %s не в PATH. Добавьте в ~/.profile строку:\n  export PATH="%s:$PATH"\n' \
        "$prefix" "$prefix" ;;
esac

if test -n "$bind"; then
    mkdir -p "$(dirname "$config")"
    printf '%s\n' "$source_root" > "$config"
    printf 'Каталог проекта привязан: %s -> %s\n' "$config" "$source_root"
elif test -e "$config"; then
    printf 'Привязка не тронута: %s -> %s\n' "$config" "$(head -1 "$config")"
    printf 'Переназначить: %s --bind\n' "${0##*/}"
else
    printf 'Привязки нет: вне каталога проекта укажите --root, задайте\n'
    printf 'AGENT_DOCKER_ROOT или выполните: bash cli/install.sh --bind\n'
fi
