#!/bin/bash
# Проверка целей vm-logs, vm-profile и vm-remove с подставным docker и временным состоянием.
set -euo pipefail
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
unset VM LOG_TAIL LOG_SERVICE LOG_FOLLOW DISK_KEEP

mkdir -p "$work/project" "$work/bin" "$work/state/vm-02"
cp -R "$source_root/Makefile" "$source_root/.env.example" "$source_root/v2" "$work/project/"
printf '2 4096 12\n' > "$work/state/vm-02/resources"
printf 'http://proxy.invalid:3128\n' > "$work/state/vm-02/upstream"
mkdir -p "$work/state/vm-03"

cat > "$work/bin/docker" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1 ${2:-}" in
    'info --format') exit 0 ;;
    'compose version') exit 0 ;;
    'wait '*|'wait') test -z "${FAKE_WAIT_FAIL:-}"; exit $? ;;
esac
all=$*
rest=${all#*compose.yaml }
# shellcheck disable=SC2086
set -- $rest
case ${1:-} in
    ps)
        if [[ $* = *vm* ]]; then printf '%s' "${FAKE_RUNNING:-}"; fi
        ;;
    exec)
        if [[ $* = *poweroff.py* && -n ${FAKE_STOP_FAIL:-} ]]; then exit 1; fi
        if [[ $* = *profile.json* ]]; then printf '{"schema_version":1,"vm_id":"vm-02"}\n'; fi
        ;;
esac
exit 0
EOF
chmod +x "$work/bin/docker"
export work PATH="$work/bin:$PATH" V2_STATE_DIR="$work/state" FAKE_DOCKER_LOG="$work/docker.log"

target() {
    : > "$FAKE_DOCKER_LOG"
    local goal=$1
    shift
    set +e
    output=$( (cd "$work/project" && env "$@" bash v2/manage.sh "$goal") 2>&1 </dev/null )
    status=$?
    set -e
}
check() { "$@" || { printf 'Провал (строка %s): %s\nКод: %s\nВывод:\n%s\n' "${BASH_LINENO[0]}" "$*" "$status" "$output" >&2; exit 1; }; }
has() { grep -qF -- "$1" <<<"$output"; }
called() { grep -qF -- "$1" "$FAKE_DOCKER_LOG"; }
lacks_call() { ! called "$1"; }

# Журналы: по умолчанию последние 50 строк обоих сервисов, без слежения.
target vm-logs VM=vm-02
check test "$status" -eq 0
check called 'logs --no-color --tail 50'
check lacks_call '--follow'

target vm-logs VM=vm-02 LOG_TAIL=all LOG_SERVICE=proxy LOG_FOLLOW=1
check test "$status" -eq 0
check called 'logs --no-color --tail all --follow proxy'

target vm-logs VM=vm-02 LOG_TAIL=много
check test "$status" -ne 0
check has 'LOG_TAIL принимает число или all.'
check lacks_call 'logs'

target vm-logs VM=vm-02 LOG_SERVICE=db
check test "$status" -ne 0
check has 'LOG_SERVICE принимает vm или proxy.'
check lacks_call 'logs'

# Профиль читается только из работающего гостя.
target vm-profile VM=vm-02
check test "$status" -ne 0
check has 'не запущена'
check lacks_call 'cat /guest/profile.json'

target vm-profile VM=vm-02 FAKE_RUNNING=container-id
check test "$status" -eq 0
check has '"vm_id":"vm-02"'
check called 'exec -T vm cat /guest/profile.json'

# Удаление по умолчанию сохраняет диск и каталог состояния.
touch -d '2001-01-01 00:00:00' "$work/state/vm-02/resources"
target vm-remove VM=vm-02
check test "$status" -eq 0
check called 'down --remove-orphans'
check lacks_call '--volumes'
check test -f "$work/state/vm-02/resources"
check has 'Диск сохранён'

# Явное удаление диска убирает и том, и каталог состояния, не трогая соседей.
target vm-remove VM=vm-02 DISK_KEEP=0
check test "$status" -eq 0
check called 'down --volumes --remove-orphans'
check test ! -e "$work/state/vm-02"
check test -d "$work/state/vm-03"
check has 'удалена вместе с диском'

# Гость без сохранённого бюджета тоже удаляется.
target vm-remove VM=vm-03 DISK_KEEP=0
check test "$status" -eq 0
check test ! -e "$work/state/vm-03"

# Работающий гость сначала выключается штатно.
mkdir -p "$work/state/vm-04"
printf '2 4096 12\n' > "$work/state/vm-04/resources"
printf 'http://proxy.invalid:3128\n' > "$work/state/vm-04/upstream"
target vm-remove VM=vm-04 FAKE_RUNNING=container-id
check test "$status" -eq 0
check called 'poweroff.py'
check called 'down --remove-orphans'

# Несуществующий гость не удаляется и не появляется в состоянии.
target vm-remove VM=vm-77
check test "$status" -ne 0
check has 'vm-77 не найдена'
check lacks_call 'down'
check test ! -e "$work/state/vm-77"

# Гость, не ответивший на штатное выключение, всё равно удаляется.
mkdir -p "$work/state/vm-05"
printf '2 4096 12\n' > "$work/state/vm-05/resources"
printf 'http://proxy.invalid:3128\n' > "$work/state/vm-05/upstream"
target vm-remove VM=vm-05 FAKE_RUNNING=container-id FAKE_STOP_FAIL=1
check test "$status" -eq 0
check has 'принудительно'
check called 'down --remove-orphans'

# То же, когда гость не завершился в отведённое время.
target vm-remove VM=vm-05 FAKE_RUNNING=container-id FAKE_WAIT_FAIL=1
check test "$status" -eq 0
check has 'принудительно'
check called 'down --remove-orphans'

# Остановка отдельной командой по-прежнему сообщает об отказе выключения.
target vm-stop VM=vm-05 FAKE_RUNNING=container-id FAKE_STOP_FAIL=1
check test "$status" -ne 0
check lacks_call 'stop proxy'

echo 'test_vm_manage_targets.sh: все проверки пройдены.'
