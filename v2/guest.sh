#!/bin/bash
set -euo pipefail
test -f /images/.ready
profile_args=(/guest/profile.json "$VM_NAME" "$VCPU" "$RAM_MB" "$DISK_GB")
existing=0
if test -e /guest/disk.qcow2; then
    existing=1
    profile_args+=(--retain-existing-identity)
fi
# Данные cloud-init новому гостю подключаются как virtio-blk, и это убирает
# из процесса QEMU эмуляцию ATAPI. Созданный ранее гость сохраняет прежний
# CD-ROM: скрипт подготовки попадает внутрь один раз через cloud-init
# write_files, поэтому у такого гостя он продолжает искать устройство так,
# как умел при создании. Запрошенный способ учитывается только при первом
# запуске: дальше решение берётся из профиля вместе с остальным
# виртуальным оборудованием. Маркер прежних версий остаётся признаком
# гостя, уже переведённого на virtio.
requested=virtio
if (( existing )) && ! test -e /guest/.seed-virtio; then
    requested=cdrom
fi
profile_args+=(--seed "$requested")
profile=$(python3 /opt/agent-docker/vm-profile.py "${profile_args[@]}")
read -r -a fields <<< "$profile"
seed_transport=${fields[0]:-}
identity=("${fields[@]:1}")
[[ $seed_transport = virtio || $seed_transport = cdrom ]] ||
    { echo 'Неизвестный способ подключения данных cloud-init в профиле VM.' >&2; exit 1; }
qemu_identity=()
network_device=virtio-net-pci,netdev=guest-net,romfile=
disk=(-drive file=/guest/disk.qcow2,if=virtio,format=qcow2)
if (( ${#identity[@]} )); then
    (( ${#identity[@]} == 2 || ${#identity[@]} == 6 )) || { echo 'Некорректная идентичность VM в профиле.' >&2; exit 1; }
    qemu_identity=(-uuid "${identity[0]}")
    network_device+=,mac=${identity[1]}
    if (( ${#identity[@]} == 6 )); then
        qemu_identity+=(-smbios "type=1,serial=${identity[2]}" -smbios "type=2,serial=${identity[3]}"
            -smbios "type=3,serial=${identity[4]}")
        disk=(-drive file=/guest/disk.qcow2,if=none,id=disk0,format=qcow2
            -device "virtio-blk-pci,drive=disk0,serial=${identity[5]}")
    fi
fi
if ! test -e /guest/disk.qcow2; then
    qemu-img create -f qcow2 -F qcow2 -b /images/base.qcow2 /guest/disk.qcow2 "${DISK_GB}G"
fi
python3 /opt/agent-docker/seed.py
genisoimage -quiet -o /guest/seed.iso -volid cidata -joliet -rock /guest/seed
rm -f /guest/serial.sock /guest/qmp.sock /guest/control.sock /guest/session.sock /guest/console.pending
# Слот seed закреплён старшим, чтобы системный диск всегда получал меньший
# номер и оставался /dev/vda. По той же причине у канала сессий адрес задан
# явно (0x1d, рядом с seed): без него новое устройство заняло бы первый
# свободный слот и сдвинуло нумерацию дисков у ранее созданного гостя.
# Канал — virtio-serial, а не сеть: портов он не открывает.
if [[ $seed_transport = cdrom ]]; then
    seed=(-drive 'file=/guest/seed.iso,media=cdrom,readonly=on')
else
    seed=(-drive 'file=/guest/seed.iso,if=none,id=seed,format=raw,readonly=on'
        -device 'virtio-blk-pci,drive=seed,addr=0x1e')
fi

# -cpu host,-vmx,-svm отключает вложенную виртуализацию внутри гостя, а
# -sandbox ограничивает системные вызовы самого QEMU: после запуска ему не
# нужны ни новые процессы, ни повышение прав, ни управление ресурсами.
# Фильтр seccomp устанавливается без CAP_SYS_ADMIN только потому, что
# start-vm.sh запускает QEMU с no-new-privs: убрав его, VM не запустится.
exec qemu-system-x86_64 \
    -accel kvm -cpu 'host,-vmx,-svm' -smp "$VCPU" -m "$RAM_MB" "${qemu_identity[@]}" \
    -sandbox 'on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny' \
    -qmp unix:/guest/qmp.sock,server=on,wait=off \
    -display none -monitor none -nodefaults \
    -chardev socket,id=serial,path=/guest/serial.sock,server=on,wait=off,logfile=/guest/console.log,logappend=off \
    -serial chardev:serial \
    -device virtio-serial-pci,id=atrice-ctl,addr=0x1d \
    -chardev socket,id=control,path=/guest/control.sock,server=on,wait=off \
    -device virtserialport,bus=atrice-ctl.0,chardev=control,name=atrice.control \
    -device virtio-serial-pci,id=atrice-ses,addr=0x1c \
    -chardev socket,id=session,path=/guest/session.sock,server=on,wait=off \
    -device virtserialport,bus=atrice-ses.0,chardev=session,name=atrice.session \
    -kernel /images/vmlinuz -initrd /images/initrd.img \
    -append 'root=/dev/vda1 ro console=ttyS0,115200' \
    "${disk[@]}" "${seed[@]}" \
    -netdev user,id=guest-net,ipv6=off -device "$network_device"
