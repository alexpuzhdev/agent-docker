#!/bin/bash
# Проверка поиска устройства с данными cloud-init в prepare-guest.sh.
# Подставляются blkid, mount и сопутствующие команды, а каталог ссылок udev
# переносится в рабочий: настоящие устройства, права и сеть не нужны.
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

mkdir -p "$work/bin" "$work/seed" "$work/dev/by-label" "$work/root/var/lib/agent-docker" "$work/root/etc"
printf 'host.example 10.0.0.1\n' > "$work/seed/allowed-hosts"
printf '127.0.0.1 localhost\n' > "$work/root/etc/hosts"
# Ссылка udev должна указывать на существующий файл: на оборванную
# ссылку скрипт не должен полагаться.
: > "$work/dev/vdc"
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
cat > "$work/bin/udevadm" <<'EOF'
#!/bin/bash
printf 'udevadm %s\n' "$*" >> "$FAKE_UDEV_LOG"
# Ссылка udev появляется только после обработки событий.
if [[ ${FAKE_UDEV_LINK:-} = 1 ]]; then ln -sfn "$FAKE_DEV/vdc" "$FAKE_DEV/by-label/cidata"; fi
EOF
cat > "$work/bin/claude" <<'EOF'
#!/bin/bash
printf 'claude 1.0.0\n'
EOF
cat > "$work/bin/tmux" <<'EOF'
#!/bin/bash
exit 0
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
    FAKE_APT_LOG="$work/apt.log" FAKE_UDEV_LOG="$work/udev.log" FAKE_DEV="$work/dev"

run() {
    : > "$FAKE_MOUNT_LOG"
    : > "$FAKE_APT_LOG"
    : > "$FAKE_UDEV_LOG"
    script=$work/prepare-guest.sh
    sed -e "s|/var/lib/agent-docker|$work/root/var/lib/agent-docker|g" \
        -e "s|/etc/hosts|$work/root/etc/hosts|g" \
        -e "s|/dev/disk/by-label|$work/dev/by-label|g" "$script_source" > "$script"
    output=$(bash "$script" 2>&1)
    status=$?
}

# Ссылка udev важнее blkid: на минимальном корне blkid может не быть.
ln -sfn "$work/dev/vdc" "$work/dev/by-label/cidata"
run
named 'подготовка завершилась успешно' test "$status" -eq 0
named 'смонтирована ссылка udev' grep -q "$work/dev/by-label/cidata" "$FAKE_MOUNT_LOG"
named 'при наличии ссылки устройство от blkid не берётся' bash -c '! grep -q "/dev/vdb" "$FAKE_MOUNT_LOG"'
named 'готовая ссылка не требует ожидания udev' test ! -s "$FAKE_UDEV_LOG"
named 'домены добавлены в hosts' grep -q 'host.example' "$work/root/etc/hosts"
named 'повторный запуск не ставит пакеты' grep -q 'AGENT_DOCKER_READY' <<<"$output"

# Без ссылки устройство ищется через blkid, тоже по метке.
rm -f "$work/dev/by-label/cidata"
run
named 'устройство найдено по метке через blkid' grep -q '/dev/vdb' "$FAKE_MOUNT_LOG"
named 'жёсткий путь CD-ROM не используется' bash -c '! grep -q "/dev/sr0" "$FAKE_MOUNT_LOG"'
named 'блок доменов не дублируется' test "$(grep -c 'host.example' "$work/root/etc/hosts")" -eq 1
named 'пакеты не ставятся повторно' test ! -s "$FAKE_APT_LOG"

# Первичная подготовка устанавливает инструменты для управляемых сессий.
command() {
    if [[ $* = '-v tmux' && ${FAKE_NO_TMUX:-} = 1 ]]; then return 1; fi
    builtin command "$@"
}
export -f command
FAKE_NO_TMUX=1 run
named 'существующая ВМ без tmux получает только tmux' grep -Eq 'install .* tmux$' "$FAKE_APT_LOG"
rm -f "$work/root/var/lib/agent-docker/ready"
FAKE_NO_TMUX=1 run
named 'новая ВМ получает Claude Code, Git и tmux' grep -Eq 'install .* claude-code git tmux$' "$FAKE_APT_LOG"

# Без blkid подготовка продолжается по ссылке udev: его отсутствие само по
# себе не повод останавливать гостя.
mv "$work/bin/blkid" "$work/blkid.disabled"
ln -sfn "$work/dev/vdc" "$work/dev/by-label/cidata"
run
named 'без blkid подготовка проходит по ссылке' test "$status" -eq 0
named 'без blkid смонтирована ссылка' grep -q "$work/dev/by-label/cidata" "$FAKE_MOUNT_LOG"
rm -f "$work/dev/by-label/cidata"
run
named 'без blkid и без ссылки подготовка не продолжается' test "$status" -ne 0
named 'без blkid в сообщении названа метка' grep -q 'cidata' <<<"$output"
mv "$work/blkid.disabled" "$work/bin/blkid"

# Ни ссылки, ни метки у blkid: причина называется прямо, монтирования нет.
FAKE_NO_LABEL=1 run
named 'отсутствие тома обрывает подготовку' test "$status" -ne 0
named 'в сообщении названа метка cidata' grep -q 'cidata' <<<"$output"
named 'монтирование не выполнялось' test ! -s "$FAKE_MOUNT_LOG"

# Ссылка может появиться позже самой службы: без blkid остаётся дождаться
# обработки событий udev и повторить поиск.
mv "$work/bin/blkid" "$work/blkid.disabled"
FAKE_UDEV_LINK=1 run
named 'после ожидания udev подготовка проходит' test "$status" -eq 0
named 'ожидание udev выполнялось' grep -q 'settle' "$FAKE_UDEV_LOG"
named 'смонтирована появившаяся ссылка' grep -q "$work/dev/by-label/cidata" "$FAKE_MOUNT_LOG"
rm -f "$work/dev/by-label/cidata"
run
named 'без появившейся ссылки подготовка не продолжается' test "$status" -ne 0
mv "$work/blkid.disabled" "$work/bin/blkid"

# Отказ монтирования должен называть выбранное устройство.
cat > "$work/bin/mount" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_MOUNT_LOG"
printf 'mount: %s: нет такого устройства\n' "${*: -2:1}" >&2
exit 32
EOF
chmod +x "$work/bin/mount"
run
named 'отказ монтирования обрывает подготовку' test "$status" -ne 0
named 'выбранное устройство попадает в журнал' grep -q 'Данные cloud-init: /dev/vdb' <<<"$output"

echo "=== Итог: неудачных проверок $failures ==="
exit $(( failures > 0 ? 1 : 0 ))
