#!/bin/bash
set -euo pipefail

gateway=$(python3 -c 'import ipaddress, sys; print(ipaddress.IPv4Address(sys.argv[1]))' "${GATEWAY_IP:?}")
nft -f - <<NFT
add table inet agent_docker
delete table inet agent_docker
table inet agent_docker {
    chain output {
        type filter hook output priority filter; policy drop;
        ip daddr $gateway tcp dport { 80, 443 } accept
    }
}
NFT

exec setpriv --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs \
    bash /opt/agent-docker/guest.sh
