#!/bin/bash
set -euo pipefail
test -f /images/.ready
profile_args=(/guest/profile.json "$VM_NAME" "$VCPU" "$RAM_MB" "$DISK_GB")
if test -e /guest/disk.qcow2; then
    profile_args+=(--retain-existing-identity)
fi
identity=$(python3 /opt/agent-docker/vm-profile.py "${profile_args[@]}")
qemu_identity=()
network_device=virtio-net-pci,netdev=guest-net,romfile=
if test -n "$identity"; then
    read -r vm_uuid vm_mac <<< "$identity"
    qemu_identity=(-uuid "$vm_uuid")
    network_device+=,mac=$vm_mac
fi
if ! test -e /guest/disk.qcow2; then
    qemu-img create -f qcow2 -F qcow2 -b /images/base.qcow2 /guest/disk.qcow2 "${DISK_GB}G"
fi
python3 /opt/agent-docker/seed.py
genisoimage -quiet -o /guest/seed.iso -volid cidata -joliet -rock /guest/seed
rm -f /guest/serial.sock /guest/qmp.sock /guest/console.pending
exec qemu-system-x86_64 \
    -accel kvm -cpu host -smp "$VCPU" -m "$RAM_MB" "${qemu_identity[@]}" \
    -qmp unix:/guest/qmp.sock,server=on,wait=off \
    -display none -monitor none -nodefaults \
    -chardev socket,id=serial,path=/guest/serial.sock,server=on,wait=off,logfile=/guest/console.log,logappend=off \
    -serial chardev:serial -kernel /images/vmlinuz -initrd /images/initrd.img \
    -append 'root=/dev/vda1 ro console=ttyS0,115200' \
    -drive 'file=/guest/disk.qcow2,if=virtio,format=qcow2' \
    -drive 'file=/guest/seed.iso,media=cdrom,readonly=on' \
    -netdev user,id=guest-net,ipv6=off -device "$network_device"
