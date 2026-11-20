#!/bin/bash
# Проверка поиска устройства с данными cloud-init в prepare-guest.sh.
# Подставляются blkid, mount и сопутствующие команды: настоящие устройства
# и сеть не нужны.
set -u
source_root=$(cd "$(dirname "$0")/../.." && pwd -P)
script_source=${1:-$source_root/v2/prepare-guest.sh}
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
failures=0
label=''
check() {
    local name=${label:-$*}
    if "$@"; then printf 'OK     %s\n' "$name"; else printf 'ПРОВАЛ (строка %s) %s\n' "${BASH_LINENO[1]}" "$name"; failures=$((failures+1)); fi
    label=''
}
named() { label=$1; shift; check "$@"; }

mkdir -p "$work/bin" "$work/seed" "$work/root/var/lib/agent-docker" "$work/root/etc"
printf 'host.example 10.0.0.1\n' > "$work/seed/allowed-hosts"
printf '127.0.0.1 localhost\n' > "$work/root/etc/hosts"
printf 'версия 1.0\n' > "$work/root/var/lib/agent-docker/ready"

# Подставные команды: монтирование подменяется копированием каталога seed,
# а claude и ready-файл обрывают скрипт до установки пакетов.
cat > "$work/bin/blkid" <<'EOF'
#!/bin/bash
# Метку знает только первый вызов с ожидаемым регистром.
if [[ ${FAKE_NO_LABEL:-} = 1 ]]; then exit 2; fi
if [[ $1 = -L && $2 = cidata ]]; then printf '/dev/vdb\n'; exit 0; fi
exit 2
EOF
cat > "$work/bin/mount" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_MOUNT_LOG"
target=${*: -1}
cp -r "$FAKE_SEED"/. "$target"/
EOF
cat > "$work/bin/umount" <<'EOF'
#!/bin/bash
# Размонтирование должно освобождать каталог, иначе rmdir в trap упадёт.
target=${1:?}
find "$target" -mindepth 1 -delete 2>/dev/null
exit 0
EOF
cat > "$work/bin/mountpoint" <<'EOF'
#!/bin/bash
exit 1
EOF
cat > "$work/bin/claude" <<'EOF'
#!/bin/bash
printf 'claude 1.0.0\n'
EOF
cat > "$work/bin/apt-get" <<'EOF'
#!/bin/bash
printf 'apt-get %s\n' "$*" >> "$FAKE_APT_LOG"
EOF
cat > "$work/bin/python3" <<'EOF'
#!/bin/bash
printf 'python3\n' >> "$FAKE_APT_LOG"
cat > /dev/null
EOF
chmod +x "$work/bin"/*
export PATH="$work/bin:$PATH" FAKE_SEED="$work/seed" FAKE_MOUNT_LOG="$work/mount.log" \
    FAKE_APT_LOG="$work/apt.log"

run() {
    : > "$FAKE_MOUNT_LOG"
    : > "$FAKE_APT_LOG"
    script=$work/prepare-guest.sh
    sed -e "s|/var/lib/agent-docker|$work/root/var/lib/agent-docker|g" \
        -e "s|/etc/hosts|$work/root/etc/hosts|g" "$script_source" > "$script"
    output=$(bash "$script" 2>&1)
    status=$?
}

# Устройство ищется по метке, а не по жёсткому пути.
FAKE_NO_LABEL= run
named 'подготовка завершилась успешно' test "$status" -eq 0
named 'смонтировано устройство, найденное по метке' grep -q '/dev/vdb' "$FAKE_MOUNT_LOG"
named 'жёсткий путь CD-ROM не используется' bash -c '! grep -q "/dev/sr0" "$FAKE_MOUNT_LOG"'
named 'домены добавлены в hosts' grep -q 'host.example' "$work/root/etc/hosts"
named 'повторный запуск не ставит пакеты' grep -q 'AGENT_DOCKER_READY' <<<"$output"

# Повторная подготовка не копит записи в hosts.
run
named 'блок доменов не дублируется' test "$(grep -c 'host.example' "$work/root/etc/hosts")" -eq 1

# Без метки остаётся откат на прежнее устройство: гость, загруженный
# старой конфигурацией, продолжает подготавливаться.
FAKE_NO_LABEL=1 run
named 'без метки используется /dev/sr0' grep -q '/dev/sr0' "$FAKE_MOUNT_LOG"

named 'пакеты не ставятся повторно' test ! -s "$FAKE_APT_LOG"

# Без метки и без прежнего устройства отказ должен быть понятным.
cat > "$work/bin/mount" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_MOUNT_LOG"
printf 'mount: %s: нет такого устройства\n' "${*: -2:1}" >&2
exit 32
EOF
chmod +x "$work/bin/mount"
FAKE_NO_LABEL=1 run
named 'отсутствие устройства обрывает подготовку' test "$status" -ne 0
named 'в сообщении видно, какое устройство не смонтировалось' grep -q '/dev/sr0' <<<"$output"
named 'выбранное устройство попадает в журнал' grep -q 'Данные cloud-init' <<<"$output"

# Без blkid причина должна называться прямо, а не прятаться за mount.
mv "$work/bin/blkid" "$work/blkid.disabled"
run
named 'отсутствие blkid названо прямо' grep -q 'Не найден blkid' <<<"$output"
named 'без blkid подготовка не продолжается' test "$status" -ne 0
mv "$work/blkid.disabled" "$work/bin/blkid"

echo "=== Итог: неудачных проверок $failures ==="
exit $(( failures > 0 ? 1 : 0 ))
