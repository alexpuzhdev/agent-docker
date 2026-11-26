FROM node:22-bookworm-slim

RUN node -e 'process.stdout.write(require("node:tls").rootCertificates.join("\n") + "\n")' > /tmp/bootstrap-ca.pem \
    && chmod 0644 /tmp/bootstrap-ca.pem \
    && sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::CAInfo=/tmp/bootstrap-ca.pem -o Acquire::Retries=0 -o Acquire::https::Timeout=40 update \
    && apt-get -o Acquire::https::CAInfo=/tmp/bootstrap-ca.pem -o Acquire::Retries=0 -o Acquire::https::Timeout=40 install -y --no-install-recommends \
        ca-certificates curl e2fsprogs fdisk genisoimage git gnupg nftables python3 qemu-system-x86 qemu-utils util-linux \
    && ldd "$(command -v qemu-system-x86_64)" | grep -q libseccomp \
    && rm -rf /var/lib/apt/lists/* /tmp/bootstrap-ca.pem

COPY *.sh *.py *.cjs *.service *.rules *.conf /opt/agent-docker/
WORKDIR /opt/agent-docker
