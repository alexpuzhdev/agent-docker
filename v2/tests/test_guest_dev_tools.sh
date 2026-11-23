#!/bin/bash
# Проверка доставки гостевых инструментов: сверка версий с go.mod, отказы и
# безопасность повторного запуска. Настоящие apt, сеть и root не нужны —
# весь набор инструментов подставной, а PATH ограничен каталогом стенда.
#
#   bash v2/tests/test_guest_dev_tools.sh
set -u
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

project=$work/project
bin=$work/bin
apt_log=$work/apt.log
apt_list=$work/agent-docker.list
mkdir -p "$project/v2" "$bin"
cp "$source_root/v2/guest-dev-tools.sh" "$project/v2/"
printf 'module agent-docker\n\ngo 1.21\n' > "$project/go.mod"
printf 'deb https://deb.debian.org/debian trixie main\n' > "$apt_list"

# В PATH стенда попадают только эти настоящие утилиты: всё остальное, что
# ищет скрипт, должно быть подставным, иначе тест зависел бы от того, что
# установлено на машине проверяющего.
for tool in bash dirname sed sort head grep cat chmod rm id; do
    ln -s "$(command -v "$tool")" "$bin/$tool"
done

# stub создаёт подставной инструмент, печатающий заданную строку версии.
stub() {
    # Цель снимается заранее: в каталоге стенда часть имён — ссылки на
    # настоящие утилиты, и запись по ссылке ушла бы в /usr/bin.
    rm -f "$bin/$1"
    printf '#!/bin/bash\nprintf %s\n' "'%s\\n' '$2'" > "$bin/$1"
    chmod +x "$bin/$1"
}

# full_set — набор, который скрипт должен признать достаточным.
full_set() {
    rm -f "$bin"/go "$bin"/gofmt "$bin"/node "$bin"/make "$bin"/gcc
    stub go 'go version go1.24.4 linux/amd64'
    stub gofmt 'gofmt'
    stub node 'v20.19.2'
    stub make 'GNU Make 4.4.1'
    stub gcc 'gcc (Debian 14.2.0-19) 14.2.0'
    stub git 'git version 2.47.3'
    stub python3 'Python 3.13.5'
    stub openssl 'OpenSSL 3.5.9 1 Jul 2025'
}

# Подставной apt-get записывает вызов и доносит пакеты так, как это сделала бы
# установка: иначе нельзя отличить удачную доставку от молчаливого отказа.
cat > "$bin/apt-get" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$APT_LOG"
for word in "$@"; do
    case $word in
        golang-go) "$STUB" go 'go version go1.24.4 linux/amd64'; "$STUB" gofmt 'gofmt' ;;
        nodejs) "$STUB" node 'v20.19.2' ;;
        make) "$STUB" make 'GNU Make 4.4.1' ;;
        gcc) "$STUB" gcc 'gcc (Debian 14.2.0-19) 14.2.0' ;;
    esac
done
exit 0
EOF
chmod +x "$bin/apt-get"

cat > "$work/stub" <<'EOF'
#!/bin/bash
rm -f "$BIN/$1"
printf '#!/bin/bash\nprintf %s\n' "'%s\\n' '$2'" > "$BIN/$1"
chmod +x "$BIN/$1"
EOF
chmod +x "$work/stub"

failures=0
label=''
check() {
    local name=${label:-$*}
    if "$@"; then
        printf 'OK     %s\n' "$name"
    else
        printf 'ПРОВАЛ (строка %s) %s\n' "${BASH_LINENO[1]}" "$name"
        printf 'Код: %s\nВывод:\n%s\n' "$status" "$output"
        failures=$((failures+1))
    fi
    label=''
}
named() { label=$1; shift; check "$@"; }
has() { grep -qF -- "$2" <<<"$1"; }
lacks() { ! has "$1" "$2"; }

run() {
    : > "$apt_log"
    output=$( (cd "$project" && env -i PATH="$bin" BIN="$bin" APT_LOG="$apt_log" \
        STUB="$work/stub" AGENT_DOCKER_APT_LIST="$apt_list" \
        bash v2/guest-dev-tools.sh "$@") 2>&1 </dev/null )
    status=$?
}

echo '=== Полный набор ==='
full_set
run --check
named 'полный набор признан достаточным' test "$status" -eq 0
named 'версия Go сверена с go.mod' has "$output" 'ок       go       1.24.4 (нужно не ниже 1.21)'
named 'про GOTOOLCHAIN сказано' has "$output" 'GOTOOLCHAIN=local'

echo '=== Несовпадение версий ==='
stub node 'v18.19.0'
run --check
named 'старый node отклонён' test "$status" -eq 1
named 'назван пакет nodejs' has "$output" 'nodejs'
named 'показана найденная версия' has "$output" '18.19.0'
full_set

printf 'module agent-docker\n\ngo 1.99\n' > "$project/go.mod"
run --check
named 'директива go из go.mod главнее' test "$status" -eq 1
named 'назван пакет golang-go' has "$output" 'golang-go'
named 'указана требуемая версия' has "$output" 'не ниже 1.99'
printf 'module agent-docker\n\ngo 1.21\n' > "$project/go.mod"

echo '=== Отсутствие инструментов ==='
rm -f "$bin/go" "$bin/gofmt"
run --check
named 'нет go — отказ' test "$status" -eq 1
named 'пакет назван один раз' test "$(grep -c -- 'golang-go' <<<"${output##*Не хватает}")" -eq 1
named 'проверка ничего не устанавливает' test ! -s "$apt_log"

echo '=== Установка без прав ==='
run --install
named 'без root установка отклонена' test "$status" -eq 1
named 'причина названа' has "$output" 'root'
named 'apt не вызывался' test ! -s "$apt_log"

echo '=== Установка от root ==='
stub id '0'
run --install
named 'установка прошла' test "$status" -eq 0
named 'apt получил только недостающий пакет' test "$(sed -n '2p' "$apt_log")" = \
    "-o Dir::Etc::sourcelist=$apt_list -o Dir::Etc::sourceparts=- -o Acquire::Retries=0 -o Acquire::https::Timeout=40 install -y --no-install-recommends golang-go"
named 'источники ограничены списком agent-docker' has "$(cat "$apt_log")" "sourcelist=$apt_list"
named 'sources.list.d не подключается' has "$(cat "$apt_log")" 'sourceparts=-'
named 'рекомендации не тянутся' has "$(cat "$apt_log")" '--no-install-recommends'
named 'ничего не удаляется' lacks "$(cat "$apt_log")" 'remove'
named 'ничего не обновляется сверх нужного' lacks "$(cat "$apt_log")" 'upgrade'
named 'проверка после установки прошла' has "$output" 'Набор полный'

echo '=== Повторный запуск ==='
run --install
named 'повторная установка не нужна' test "$status" -eq 0
named 'apt не вызывался повторно' test ! -s "$apt_log"
named 'сказано, что набор полный' has "$output" 'Набор полный'

echo '=== Нет списка источников ==='
rm -f "$bin/node" "$apt_list"
run --install
named 'без списка источников установка отклонена' test "$status" -eq 1
named 'назван отсутствующий список' has "$output" "$apt_list"
named 'apt не вызывался' test ! -s "$apt_log"

printf '=== Итог: неудачных проверок %s ===\n' "$failures"
test "$failures" -eq 0
