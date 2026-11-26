#!/bin/bash
# Проверка узкой установки канала сессий на существующего гостя.
#
#   bash v2/tests/test_install_sessions_guest.sh
#
# Установка идёт в подставной корень через --root, поэтому root, настоящий
# гость, apt и сеть не нужны. Главное, что проверяется: записаны ровно пять
# файлов (служба, юнит, правило udev, файл tmpfiles с каталогом сокета и
# profile.d с его путём), повторный запуск не пишет ничего, и ни один чужой
# файл не тронут — в том числе ключи хоста, учётные данные, профиль и
# /etc/hosts.
#
# Первым аргументом можно передать путь к проверяемому install-sessions-guest.sh.
set -u
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
script=${1:-$source_root/v2/install-sessions-guest.sh}
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

guest=$work/guest
mkdir -p "$guest/etc/ssh" "$guest/etc/systemd/system" "$guest/etc/udev/rules.d" \
    "$guest/home/agent/.claude" "$guest/var/lib/agent-docker" "$guest/usr/local/lib"

# Чужие файлы, которых установка касаться не должна.
protected=(
    "etc/ssh/ssh_host_ed25519_key"
    "etc/ssh/ssh_host_ed25519_key.pub"
    "etc/hosts"
    "home/agent/.claude/.credentials.json"
    "var/lib/agent-docker/ready"
    "etc/machine-id"
)
for path in "${protected[@]}"; do
    mkdir -p "$guest/$(dirname "$path")"
    printf 'содержимое %s\n' "$path" > "$guest/$path"
done
before=$work/before.sums
( cd "$guest" && sha256sum "${protected[@]}" ) > "$before"

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
    output=$(bash "$script" "$@" --root "$guest" 2>&1 </dev/null)
    status=$?
}

responder=$guest/usr/local/lib/atrice/sessions-responder.py
unit=$guest/etc/systemd/system/atrice-sessions.service
rule=$guest/etc/udev/rules.d/60-atrice-control.rules
tmpfiles=$guest/usr/lib/tmpfiles.d/atrice-sessions.conf
profile=$guest/etc/profile.d/atrice-tmux.sh

echo '=== Проверка до установки ==='
run --check
named 'неустановленный канал определён' test "$status" -eq 1
named 'названы отсутствующие файлы' has "$output" 'нет        /usr/local/lib/atrice'
named 'проверка ничего не создала' test ! -e "$responder"

echo '=== Установка ==='
run --install
named 'установка прошла без root' test "$status" -eq 0
named 'служба записана' test -f "$responder"
named 'юнит записан' test -f "$unit"
named 'правило udev записано' test -f "$rule"
named 'файл tmpfiles записан' test -f "$tmpfiles"
named 'profile.d с путём сокета записан' test -f "$profile"
named 'права службы 0755' test "$(stat -c %a "$responder")" = 755
named 'права юнита 0644' test "$(stat -c %a "$unit")" = 644
named 'служба совпадает с исходником' cmp -s "$source_root/v2/sessions-responder.py" "$responder"
named 'юнит совпадает с исходником' cmp -s "$source_root/v2/atrice-sessions.service" "$unit"
named 'правило совпадает с исходником' cmp -s "$source_root/v2/atrice-control.rules" "$rule"
named 'tmpfiles совпадает с исходником' \
    cmp -s "$source_root/v2/atrice-sessions.tmpfiles.conf" "$tmpfiles"
named 'profile.d совпадает с исходником' \
    cmp -s "$source_root/v2/atrice-tmux-profile.sh" "$profile"
named 'права tmpfiles 0644' test "$(stat -c %a "$tmpfiles")" = 644
named 'права profile.d 0644' test "$(stat -c %a "$profile")" = 644
# Каталог сокета задан один и тот же по обе стороны: служба смотрит ровно
# этот путь, агент запускает tmux ровно на нём.
socket_path=$(sed -n 's/^ATRICE_TMUX_SOCKET=//p' "$profile")
named 'путь сокета задан' test -n "$socket_path"
named 'каталог сокета создаётся заранее' \
    grep -qF -- "d $(dirname "$socket_path")" "$tmpfiles"
named 'каталог сокета закрыт для посторонних' grep -qE '^d .*0770 agent agent' "$tmpfiles"
named 'служба смотрит тот же путь' \
    grep -qF -- "'$socket_path'" "$source_root/v2/sessions-responder.py"
named 'сказано про следующий запуск' has "$output" 'следующем запуске'
named 'sudo не упоминается' lacks "$output" 'sudo'

echo '=== Чужие файлы не тронуты ==='
named 'контрольные суммы совпадают' bash -c "cd '$guest' && sha256sum -c --status '$before'"
named 'временных файлов не осталось' test -z "$(find "$guest" -name '.atrice.*' -print -quit)"
named 'новых путей ровно пять' test "$(find "$guest/usr/local/lib/atrice" "$guest/etc/systemd/system" \
    "$guest/etc/udev/rules.d" "$guest/usr/lib/tmpfiles.d" "$guest/etc/profile.d" \
    -type f | wc -l)" -eq 5

echo '=== Повторный запуск ==='
stamp=$(stat -c %Y "$unit")
run --install
named 'повторная установка прошла' test "$status" -eq 0
named 'ничего не записано' has "$output" 'ничего не записано'
named 'время файла не изменилось' test "$(stat -c %Y "$unit")" = "$stamp"
named 'чужие файлы по-прежнему целы' bash -c "cd '$guest' && sha256sum -c --status '$before'"

echo '=== Проверка после установки ==='
run --check
named 'канал признан установленным' test "$status" -eq 0
named 'все пять файлов на месте' test "$(grep -c '^на месте' <<<"$output")" -eq 5

echo '=== Расхождение содержимого ==='
printf 'чужая правка\n' > "$unit"
run --check
named 'расхождение замечено' test "$status" -eq 1
named 'назван отличающийся файл' has "$output" 'отличается /etc/systemd/system'
run --install
named 'расхождение исправлено' cmp -s "$source_root/v2/atrice-sessions.service" "$unit"

echo '=== Отказы ==='
output=$(bash "$script" --root "$guest" 2>&1 </dev/null); status=$?
named 'без режима отказ' test "$status" -eq 2
output=$(bash "$script" --install --root "$work/нет-такого" 2>&1 </dev/null); status=$?
named 'несуществующий корень отклонён' test "$status" -eq 2
output=$(bash "$script" --install --неизвестный --root "$guest" 2>&1 </dev/null); status=$?
named 'неизвестный флаг отклонён' test "$status" -eq 2

echo '=== Включение юнита в офлайн-режиме ==='
wants=etc/systemd/system/multi-user.target.wants/atrice-sessions.service
named 'ссылка включения создана' test -L "$guest/$wants"
named 'ссылка ведёт на путь юнита внутри гостя' \
    test "$(readlink -- "$guest/$wants")" = /etc/systemd/system/atrice-sessions.service
if command -v systemctl >/dev/null; then
    output=$(systemctl --root="$guest" is-enabled atrice-sessions.service 2>&1); status=$?
    named 'systemctl --root показывает enabled' test "$output" = enabled
else
    echo 'ПРОПУЩЕНО systemctl --root: systemctl недоступен'
fi

echo '=== Повторное включение ничего не меняет ==='
link_stamp=$(stat -c %Y "$guest/$wants")
run --install
named 'повтор прошёл' test "$status" -eq 0
named 'сказано, что юнит уже включён' has "$output" 'уже включён'
named 'время ссылки не изменилось' test "$(stat -c %Y "$guest/$wants")" = "$link_stamp"
named 'цель ссылки не изменилась' \
    test "$(readlink -- "$guest/$wants")" = /etc/systemd/system/atrice-sessions.service

# fresh_guest готовит отдельный пустой корень: случаи со ссылками нельзя
# проверять на уже установленном дереве.
fresh_guest() {
    local directory
    directory=$(mktemp -d "$work/guest.XXXXXX")
    mkdir -p "$directory/etc"
    printf '%s' "$directory"
}

# outside — безопасная временная директория вне корня с контрольным файлом.
outside=$(mktemp -d "$work/outside.XXXXXX")
printf 'контрольное содержимое\n' > "$outside/контроль"
outside_sum=$(sha256sum "$outside/контроль" | cut -d' ' -f1)
outside_count=$(find "$outside" | wc -l)

refuses() {
    local label_text=$1 directory=$2
    output=$(bash "$script" --install --root "$directory" 2>&1 </dev/null); status=$?
    named "$label_text: отказ" test "$status" -eq 3
    named "$label_text: причина названа" has "$output" 'Отказ:'
    named "$label_text: контрольный файл цел" \
        test "$(sha256sum "$outside/контроль" | cut -d' ' -f1)" = "$outside_sum"
    named "$label_text: вне корня ничего не создано" \
        test "$(find "$outside" | wc -l)" -eq "$outside_count"
    # Проверка тоже не должна читать сквозь подложенную ссылку.
    output=$(bash "$script" --check --root "$directory" 2>&1 </dev/null); status=$?
    named "$label_text: проверка тоже отказывает" test "$status" -eq 3
}

echo '=== Ссылка в родительском каталоге ==='
victim=$(fresh_guest)
mkdir -p "$victim/etc/systemd"
ln -s "$outside" "$victim/etc/systemd/system"
refuses 'родительский каталог' "$victim"

echo '=== Ссылка вместо целевого файла ==='
victim=$(fresh_guest)
mkdir -p "$victim/etc/systemd/system"
ln -s "$outside/контроль" "$victim/etc/systemd/system/atrice-sessions.service"
refuses 'целевой файл' "$victim"

echo '=== Ссылка в пути включения ==='
victim=$(fresh_guest)
mkdir -p "$victim/etc/systemd/system"
ln -s "$outside" "$victim/etc/systemd/system/multi-user.target.wants"
refuses 'путь включения' "$victim"

echo '=== Чужая ссылка включения ==='
victim=$(fresh_guest)
mkdir -p "$victim/etc/systemd/system/multi-user.target.wants"
ln -s "$outside/контроль" "$victim/etc/systemd/system/multi-user.target.wants/atrice-sessions.service"
refuses 'чужая ссылка включения' "$victim"

echo '=== Каталог вместо целевого файла ==='
victim=$(fresh_guest)
mkdir -p "$victim/etc/udev/rules.d/60-atrice-control.rules"
output=$(bash "$script" --install --root "$victim" 2>&1 </dev/null); status=$?
named 'каталог на месте файла отклонён' test "$status" -eq 3
named 'причина про обычный файл' has "$output" 'обычным файлом'

echo '=== Проверка ничего не создаёт ==='
victim=$(fresh_guest)
run_on() { output=$(bash "$script" "$@" 2>&1 </dev/null); status=$?; }
run_on --check --root "$victim"
named 'проверка на пустом корне отказывает' test "$status" -eq 1
named 'каталог включения не создан' test ! -e "$victim/etc/systemd/system/multi-user.target.wants"
named 'файлов не появилось' test "$(find "$victim" -type f | wc -l)" -eq 0

echo '=== Корень обязателен и не / ==='
run_on --install --root /
named 'корень / отклонён' test "$status" -eq 2
named 'причина про корень' has "$output" 'не /'
run_on --install --root ''
named 'пустой корень отклонён' test "$status" -eq 2

echo '=== Прямой вход ставится только по явному флагу ==='
direct=$(fresh_guest)
run_direct() { output=$(bash "$script" "$@" 2>&1 </dev/null); status=$?; }
direct_service=$direct/usr/local/lib/atrice/session-responder.py
direct_unit=$direct/etc/systemd/system/atrice-session.service
direct_rule=$direct/etc/udev/rules.d/60-atrice-session.rules
direct_link=$direct/etc/systemd/system/multi-user.target.wants/atrice-session.service
run_direct --install --root "$direct"
named 'обычная установка прошла' test "$status" -eq 0
named 'служба прямого входа не поставлена' test ! -e "$direct_service"
named 'юнит прямого входа не поставлен' test ! -e "$direct_unit"
named 'правило прямого входа не поставлено' test ! -e "$direct_rule"
named 'прямой вход не включён' test ! -e "$direct_link"
named 'канал списка включён' test -L "$direct/etc/systemd/system/multi-user.target.wants/atrice-sessions.service"

with=$(fresh_guest)
with_service=$with/usr/local/lib/atrice/session-responder.py
with_unit=$with/etc/systemd/system/atrice-session.service
with_rule=$with/etc/udev/rules.d/60-atrice-session.rules
with_link=$with/etc/systemd/system/multi-user.target.wants/atrice-session.service
run_direct --install --root "$with" --with-direct
named 'установка с флагом прошла' test "$status" -eq 0
named 'служба прямого входа записана' test -f "$with_service"
named 'юнит прямого входа записан' test -f "$with_unit"
server_unit=$with/etc/systemd/system/atrice-tmux-server.service
named 'сервер tmux установлен отдельным юнитом' test -f "$server_unit"
named 'прямой вход запускается после сервера tmux' grep -qF 'After=atrice-tmux-server.service' "$with_unit"
named 'прямой вход подтягивает сервер tmux' grep -qF 'Requires=atrice-tmux-server.service' "$with_unit"
named 'правило прямого входа записано' test -f "$with_rule"
named 'прямой вход включён ссылкой' test -L "$with_link"
named 'ссылка ведёт на юнит гостя' \
    test "$(readlink -- "$with_link")" = /etc/systemd/system/atrice-session.service
named 'канал списка тоже включён' test -L "$with/etc/systemd/system/multi-user.target.wants/atrice-sessions.service"
named 'файлов ровно девять' test "$(find "$with" -type f | wc -l)" -eq 9
run_direct --install --root "$with" --with-direct
named 'повторная установка ничего не пишет' has "$output" 'ничего не записано'
run_direct --check --root "$with" --with-direct
named 'проверка с флагом довольна' test "$status" -eq 0
run_direct --check --root "$direct" --with-direct
named 'без службы проверка с флагом отказывает' test "$status" -eq 1
named 'названа отсутствующая служба' has "$output" 'session-responder.py'

printf '=== Итог: неудачных проверок %s ===\n' "$failures"
test "$failures" -eq 0
