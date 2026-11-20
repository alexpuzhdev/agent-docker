#!/bin/bash
set -euo pipefail

expected=/var/lib/agent-docker/ready
mkdir -p "$(dirname "$expected")"
scratch=$(mktemp -d)
trap 'if mountpoint -q "$scratch"; then umount "$scratch"; fi; rmdir -- "$scratch"' EXIT
# У нового гостя данные cloud-init подключены как virtio-blk, у созданного
# ранее — как CD-ROM, поэтому устройство ищется по метке образа, а не по
# фиксированному пути. Выбранное устройство попадает в журнал: без этого
# отказ монтирования трудно отличить от отсутствия устройства.
command -v blkid >/dev/null ||
    { echo 'Не найден blkid: устройство с данными cloud-init не определить.' >&2; exit 1; }
seed_device=$(blkid -L cidata 2>/dev/null || blkid -L CIDATA 2>/dev/null || true)
test -n "$seed_device" || seed_device=/dev/sr0
printf 'Данные cloud-init: %s\n' "$seed_device"
mount -o ro "$seed_device" "$scratch"
sed -i '/^# agent-docker domains$/,/^# end agent-docker domains$/d' /etc/hosts
printf '\n# agent-docker domains\n' >> /etc/hosts
cat "$scratch/allowed-hosts" >> /etc/hosts
printf '# end agent-docker domains\n' >> /etc/hosts
umount "$scratch"

if command -v claude >/dev/null && test -f "$expected"; then
    printf 'AGENT_DOCKER_READY %s\n' "$(claude --version)"
    exit
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
apt-get "${apt_options[@]}" install -y --no-install-recommends claude-code git
claude --version > "$expected"
printf 'AGENT_DOCKER_READY %s\n' "$(cat "$expected")"
