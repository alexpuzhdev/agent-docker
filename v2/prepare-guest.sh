#!/bin/bash
set -euo pipefail

expected=/var/lib/agent-docker/ready
mkdir -p "$(dirname "$expected")"
scratch=$(mktemp -d)
trap 'if mountpoint -q "$scratch"; then umount "$scratch"; fi; rmdir -- "$scratch"' EXIT
# У нового гостя данные cloud-init подключены как virtio-blk, у созданного
# ранее — как CD-ROM, поэтому устройство ищется по метке образа, а не по
# фиксированному пути. Сначала берётся ссылка udev, blkid остаётся запасным
# способом: на минимальном корне его может не быть. Если ссылки ещё нет,
# служба дожидается обработки событий udev и повторяет поиск. Жёсткого пути
# CD-ROM здесь нет намеренно: гость, которому достался этот скрипт, получает
# данные как virtio-blk, и монтирование /dev/sr0 назвало бы ложную причину
# отказа. Выбранное устройство попадает в журнал: без этого отказ
# монтирования трудно отличить от отсутствия устройства.
find_seed() {
    local label
    for label in cidata CIDATA; do
        if test -e "/dev/disk/by-label/$label"; then
            printf '%s\n' "/dev/disk/by-label/$label"
            return 0
        fi
    done
    command -v blkid >/dev/null || return 0
    blkid -L cidata 2>/dev/null || blkid -L CIDATA 2>/dev/null || true
}
seed_device=$(find_seed)
if test -z "$seed_device" && command -v udevadm >/dev/null; then
    udevadm settle --timeout=5 || true
    seed_device=$(find_seed)
fi
test -n "$seed_device" ||
    { echo 'Не найден том с меткой cidata: данные cloud-init не подключены.' >&2; exit 1; }
printf 'Данные cloud-init: %s\n' "$seed_device"
mount -o ro "$seed_device" "$scratch"
sed -i '/^# agent-docker domains$/,/^# end agent-docker domains$/d' /etc/hosts
printf '\n# agent-docker domains\n' >> /etc/hosts
cat "$scratch/allowed-hosts" >> /etc/hosts
printf '# end agent-docker domains\n' >> /etc/hosts
umount "$scratch"

packages=(claude-code git tmux)
if command -v claude >/dev/null && test -f "$expected"; then
    if command -v tmux >/dev/null; then
        printf 'AGENT_DOCKER_READY %s\n' "$(claude --version)"
        exit
    fi
    packages=(tmux)
fi
python3 - <<'PY'
import socket
import time
import urllib.request

try:
    direct = socket.create_connection(('1.1.1.1', 443), timeout=3)
except OSError:
    pass
else:
    direct.close()
    raise SystemExit('Обнаружен прямой выход в сеть, установка отменена.')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
target = 'https://' + 'downloads.claude.ai' + '/claude-code/apt/stable/dists/stable/InRelease'
for i in range(3):
    with opener.open(target, timeout=10) as response:
        if response.status != 200:
            raise SystemExit('Шлюз недоступен, установка отменена.')
    if i < 2:
        time.sleep(1)
PY

apt_options=(
    -o Dir::Etc::sourcelist=/etc/apt/agent-docker.list
    -o Dir::Etc::sourceparts=-
    -o Acquire::Retries=0
    -o Acquire::https::Timeout=40
)
apt-get "${apt_options[@]}" update
apt-get "${apt_options[@]}" install -y --no-install-recommends "${packages[@]}"
claude --version > "$expected"
printf 'AGENT_DOCKER_READY %s\n' "$(cat "$expected")"
