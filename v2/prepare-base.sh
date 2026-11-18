#!/bin/bash
set -euo pipefail
exec 9>/images/.prepare.lock
flock 9
if test -f /images/.ready; then exit; fi
scratch=$(mktemp -d /images/prepare.XXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
cd "$scratch"
release=debian-13-genericcloud-amd64.qcow2
base=https://cloud.debian.org/images/cloud/trixie/latest
fetch() { curl -q --noproxy '' --fail --silent --show-error --location --connect-timeout 10 --max-time 480 "$@"; }
fetch "$base/SHA512SUMS" -o SHA512SUMS
awk -v file="$release" '$2==file || $2=="*" file {print; found=1} END{if(!found) exit 1}' SHA512SUMS > check.sums
fetch "$base/$release" -o "$release"
sha512sum --check check.sums
qemu-img convert -O raw "$release" disk.raw
read -r start count < <(sfdisk --json disk.raw | python3 -c '
import json, sys
table = json.load(sys.stdin)["partitiontable"]
root = max(table["partitions"], key=lambda partition: partition["size"])
print(root["start"] * table["sectorsize"], root["size"] * table["sectorsize"])
')
dd if=disk.raw of=root.ext4 iflag=skip_bytes,count_bytes skip="$start" count="$count" bs=4M conv=sparse status=none
debugfs -R 'ls -p /boot' root.ext4 2>/dev/null > boot-files
for component in vmlinuz initrd.img; do
    kernel_file=$(awk -F/ -v component="$component" 'index($6, component "-")==1 {print $6; exit}' boot-files)
    test -n "$kernel_file"
    debugfs -R "dump /boot/$kernel_file $component" root.ext4
    test -s "$component"
done
fetch https://downloads.claude.ai/keys/claude-code.asc -o claude-code.asc
mkdir -m 0700 gpg
fingerprint=$(gpg --homedir "$scratch/gpg" --batch --with-colons --show-keys claude-code.asc 2>/dev/null | awk -F: '$1=="fpr" && !seen++ {print $10}')
test "$fingerprint" = 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
mv -- "$release" /images/base.qcow2
mv -- vmlinuz initrd.img claude-code.asc /images/
touch /images/.ready
