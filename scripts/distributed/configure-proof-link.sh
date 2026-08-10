#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
    cat <<'EOF'
usage: configure-proof-link.sh --role x86|pi --interface IFACE
       [--x86-address 10.42.0.1/24] [--pi-address 10.42.0.2/24]
       [--manager auto|NetworkManager|systemd-networkd] [--apply]
       [--force-existing] [--skip-peer-test] [--require-no-default]

Without --apply this prints the detected manager and proposed configuration.
The direct-link profile never configures DHCP, DNS, a gateway, forwarding, or
NAT, and never modifies the WiFi connection profile.
EOF
}

role= interface= manager=auto apply=0 force_existing=0 skip_peer_test=0
require_no_default=0 x86_address=10.42.0.1/24 pi_address=10.42.0.2/24
while (($#)); do
    case "$1" in
        --role) role=${2:?}; shift 2 ;;
        --interface) interface=${2:?}; shift 2 ;;
        --x86-address) x86_address=${2:?}; shift 2 ;;
        --pi-address) pi_address=${2:?}; shift 2 ;;
        --manager) manager=${2:?}; shift 2 ;;
        --apply) apply=1; shift ;;
        --force-existing) force_existing=1; shift ;;
        --skip-peer-test) skip_peer_test=1; shift ;;
        --require-no-default) require_no_default=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) proof_die "unknown argument: $1" ;;
    esac
done
[[ "$role" == x86 || "$role" == pi ]] || proof_die '--role must be x86 or pi'
[[ -n "$interface" ]] || proof_die '--interface is required'
[[ -d "/sys/class/net/$interface" ]] || proof_die "interface not found: $interface"
[[ ! -d "/sys/class/net/$interface/wireless" ]] \
    || proof_die "refusing to configure WiFi interface: $interface"
proof_require_command ip
proof_require_command ping
proof_require_command systemctl

nm_active=0 networkd_active=0
systemctl is-active --quiet NetworkManager.service && nm_active=1 || true
systemctl is-active --quiet systemd-networkd.service && networkd_active=1 || true
if [[ "$manager" == auto ]]; then
    if ((nm_active && networkd_active)); then
        proof_die 'both NetworkManager and systemd-networkd are active; select the owner explicitly'
    elif ((nm_active)); then
        manager=NetworkManager
    elif ((networkd_active)); then
        manager=systemd-networkd
    else
        proof_die 'no supported active network manager detected; configure the static link manually'
    fi
fi
[[ "$manager" == NetworkManager || "$manager" == systemd-networkd ]] \
    || proof_die "unsupported manager: $manager"
if [[ "$manager" == NetworkManager && $nm_active -eq 0 ]]; then
    proof_die 'NetworkManager was selected but its service is not active'
fi
if [[ "$manager" == systemd-networkd && $networkd_active -eq 0 ]]; then
    proof_die 'systemd-networkd was selected but its service is not active'
fi

if [[ "$role" == x86 ]]; then
    address=$x86_address
    peer=${pi_address%/*}
else
    address=$pi_address
    peer=${x86_address%/*}
fi
mac=$(<"/sys/class/net/$interface/address")
default_before=$(ip -4 route show default | sort || true)
active_connection=
if ((nm_active)); then
    active_connection=$(nmcli -g GENERAL.CONNECTION device show "$interface" 2>/dev/null || true)
fi

printf 'role=%s manager=%s interface=%s mac=%s address=%s peer=%s\n' \
    "$role" "$manager" "$interface" "$mac" "$address" "$peer"
if ((apply == 0)); then
    printf '%s\n' 'dry-run only; rerun with --apply after checking the interface and WiFi route'
    exit 0
fi
[[ $EUID -eq 0 ]] || proof_die '--apply must run as root'

if [[ "$manager" == NetworkManager ]]; then
    proof_require_command nmcli
    profile=ota-proof-link
    if [[ -n "$active_connection" && "$active_connection" != -- \
            && "$active_connection" != "$profile" && $force_existing -eq 0 ]]; then
        proof_die "interface has active profile '$active_connection'; use --force-existing after review"
    fi
    if nmcli -t -f NAME connection show | grep -Fxq "$profile"; then
        bound=$(nmcli -g connection.interface-name connection show "$profile")
        [[ "$bound" == "$interface" ]] \
            || proof_die "existing $profile is bound to $bound, not $interface"
    else
        nmcli connection add type ethernet ifname "$interface" con-name "$profile"
    fi
    nmcli connection modify "$profile" \
        connection.interface-name "$interface" \
        connection.autoconnect yes \
        ipv4.method manual \
        ipv4.addresses "$address" \
        ipv4.gateway '' \
        ipv4.routes '' \
        ipv4.dns '' \
        ipv4.never-default yes \
        ipv4.ignore-auto-routes yes \
        ipv4.ignore-auto-dns yes \
        ipv6.method disabled
    nmcli connection up "$profile"
else
    proof_require_command networkctl
    target=/etc/systemd/network/10-ota-proof-link.network
    if [[ -e "$target" ]]; then
        existing_mac=$(awk -F= '$1 == "MACAddress" { print $2 }' "$target")
        [[ "$existing_mac" == "$mac" ]] \
            || proof_die "$target belongs to MAC ${existing_mac:-unknown}, not $mac"
    fi
    temporary=$(mktemp "${target}.tmp.XXXXXX")
    trap 'rm -f "$temporary"' EXIT
    {
        printf '%s\n' '[Match]'
        printf 'MACAddress=%s\n\n' "$mac"
        printf '%s\n' '[Link]'
        printf '%s\n\n' 'RequiredForOnline=no'
        printf '%s\n' '[Network]'
        printf 'Address=%s\n' "$address"
        printf '%s\n' 'DHCP=no'
        printf '%s\n' 'IPv6AcceptRA=no'
        printf '%s\n' 'LinkLocalAddressing=no'
        printf '%s\n' 'ConfigureWithoutCarrier=yes'
    } >"$temporary"
    chmod 0644 "$temporary"
    install -o root -g root -m 0644 "$temporary" "$target"
    networkctl reload
    networkctl reconfigure "$interface"
fi

ip -4 address show dev "$interface"
route_to_peer=$(ip -4 route get "$peer")
printf '%s\n' "$route_to_peer"
[[ "$route_to_peer" == *" dev $interface "* \
        || "$route_to_peer" == *" dev $interface" ]] \
    || proof_die "route to $peer does not use $interface"
source_address=${address%/*}
[[ "$route_to_peer" == *" src $source_address "* \
        || "$route_to_peer" == *" src $source_address" ]] \
    || proof_die "route to $peer does not use source $source_address"
default_after=$(ip -4 route show default | sort || true)
[[ "$default_before" == "$default_after" ]] \
    || proof_die 'default route changed while configuring the proof link'
forwarding=$(sysctl -n net.ipv4.ip_forward)
[[ "$forwarding" == 0 ]] \
    || proof_die 'IPv4 forwarding is enabled; this topology does not require routing or NAT'
if ((require_no_default)) && [[ -n "$default_after" ]]; then
    proof_die 'a default IPv4 route remains; remove it before isolated Pi proof execution'
fi
if ((skip_peer_test == 0)); then
    ping -c 3 -W 2 -I "$interface" "$peer"
fi
printf 'proof link configured without a gateway or default route: %s\n' "$address"
