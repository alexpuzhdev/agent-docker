#!/bin/bash
# Инструменты разработки для гостевой VM: проверка версий и доставка
# недостающего из разрешённых источников.
#
#   bash v2/guest-dev-tools.sh             только проверка, права root не нужны
#   bash v2/guest-dev-tools.sh --check     то же самое явно
#   bash v2/guest-dev-tools.sh --install   установка недостающего, нужен root
#
# Источники те же, что у prepare-guest.sh: список /etc/apt/agent-docker.list,
# то есть deb.debian.org и downloads.claude.ai через upstream-прокси гостя.
# Скрипт не добавляет репозитории, не трогает /etc/hosts, прокси и учётные
# данные и ничего не удаляет. При полном наборе инструментов apt не вызывается
# вовсе, поэтому повторный запуск безопасен и на уже настроенном госте.
#
# Минимальная версия Go берётся из go.mod, а не записана здесь: иначе она
# разошлась бы с проектом при следующем обновлении директивы go.
set -euo pipefail

source_root=$(cd "$(dirname "$0")/.." && pwd -P)
sources_list=${AGENT_DOCKER_APT_LIST:-/etc/apt/agent-docker.list}
mode=${1:---check}
case $mode in
    --check|--install) ;;
    *) printf 'Использование: %s [--check|--install]\n' "${0##*/}" >&2; exit 2 ;;
esac

go_minimum=$(sed -n 's/^go \([0-9][0-9.]*\).*/\1/p' "$source_root/go.mod" | head -1)
test -n "$go_minimum" ||
    { echo 'В go.mod нет директивы go: минимальная версия Go неизвестна.' >&2; exit 1; }

# Инструмент | минимальная версия | пакет Debian | команда получения версии.
# Прочерк в версии означает, что достаточно самого наличия инструмента.
# gofmt и gcc приходят вместе с другими пакетами, но проверяются отдельно:
# gofmt вызывает AGENTS.md напрямую, а без gcc не работает go test -race.
requirements=(
    "go|$go_minimum|golang-go|go version"
    "gofmt|-|golang-go|-"
    "node|20|nodejs|node --version"
    "make|4|make|make --version"
    "gcc|-|gcc|gcc --version"
    "git|2.39|git|git --version"
    "python3|3.11|python3|python3 --version"
    "openssl|3|openssl|openssl version"
)

# at_least сравнивает версии правилами sort -V; равная версия подходит.
at_least() {
    test "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2"
}

# version_of берёт первое число вида 1.2.3 из первой строки вывода команды.
version_of() {
    local output
    output=$("$@" 2>/dev/null | head -1) || return 1
    printf '%s\n' "$output" | grep -oE '[0-9]+(\.[0-9]+)*' | head -1
}

# inspect заполняет needed пакетами, которых не хватает или которые старше
# требуемого, и печатает по строке на инструмент.
needed=()
inspect() {
    local line tool minimum package probe found known
    needed=()
    for line in "${requirements[@]}"; do
        IFS='|' read -r tool minimum package probe <<<"$line"
        if ! command -v "$tool" >/dev/null; then
            printf 'нет      %-8s пакет %s\n' "$tool" "$package"
        elif test "$minimum" = '-'; then
            printf 'ок       %-8s на месте\n' "$tool"
            continue
        else
            # shellcheck disable=SC2086
            found=$(version_of $probe) || found=''
            if test -z "$found"; then
                printf 'неясно   %-8s версия не определилась, пакет %s\n' "$tool" "$package"
            elif at_least "$found" "$minimum"; then
                printf 'ок       %-8s %s (нужно не ниже %s)\n' "$tool" "$found" "$minimum"
                continue
            else
                printf 'старый   %-8s %s, нужно не ниже %s, пакет %s\n' \
                    "$tool" "$found" "$minimum" "$package"
            fi
        fi
        known=''
        for name in ${needed[@]+"${needed[@]}"}; do
            test "$name" = "$package" && known=1
        done
        test -n "$known" || needed+=("$package")
    done
}

inspect
if test "${#needed[@]}" -eq 0; then
    echo 'Набор полный: проверки из AGENTS.md можно запускать в этом госте.'
    # Прямого выхода в сеть у гостя нет, а go.mod с более новой директивой
    # заставил бы Go скачивать себе тулчейн. GOTOOLCHAIN=local превращает
    # такое расхождение в понятный отказ вместо попытки загрузки.
    echo 'Проверки Go запускайте с GOTOOLCHAIN=local.'
    exit 0
fi

printf 'Не хватает пакетов: %s\n' "${needed[*]}"
if test "$mode" = '--check'; then
    printf 'Установка: bash v2/guest-dev-tools.sh --install (от root на стенде)\n'
    exit 1
fi

test "$(id -u)" -eq 0 ||
    { echo 'Установка требует прав root: запустите её от root на отдельном стенде.' >&2; exit 1; }
test -r "$sources_list" ||
    { printf 'Нет списка источников %s: другие репозитории скрипт не добавляет.\n' \
        "$sources_list" >&2; exit 1; }

# Те же ограничения, что в prepare-guest.sh: читается только список
# agent-docker, sources.list.d не подключается, повторов загрузки нет.
apt_options=(
    -o Dir::Etc::sourcelist="$sources_list"
    -o Dir::Etc::sourceparts=-
    -o Acquire::Retries=0
    -o Acquire::https::Timeout=40
)
apt-get "${apt_options[@]}" update
apt-get "${apt_options[@]}" install -y --no-install-recommends "${needed[@]}"

echo '--- проверка после установки ---'
inspect
test "${#needed[@]}" -eq 0 ||
    { printf 'После установки всё ещё не хватает: %s\n' "${needed[*]}" >&2; exit 1; }
echo 'Набор полный. Проверки Go запускайте с GOTOOLCHAIN=local.'
