#!/usr/bin/env bash
set -euo pipefail

readonly app_root="${APP_ROOT:-/workspace/app}"
readonly output="${app_root}/artifacts/unprivileged"
mkdir -p "$output"

id > "$output/id.txt"
grep -E 'Cap(Inh|Prm|Eff|Bnd|Amb)' /proc/self/status > "$output/capabilities.txt"
ls -la /dev > "$output/devices.txt"
cat /proc/self/status > "$output/process-status.txt"
cat /proc/1/comm > "$output/pid1-comm.txt"
cat /proc/net/dev > "$output/network-devices.txt"
cat /proc/self/mountinfo > "$output/mountinfo.txt"

readonly sealed_paths=(
    "$app_root"
    "$app_root/fixtures"
    "$app_root/fixtures/sealed-v1-signed.bin"
    "$app_root/fixtures/v2-auto-confirm-signed.bin"
    "$app_root/renode"
    "$app_root/renode/FaultInjectingFlash.cs"
    "$app_root/controller"
    "$app_root/controller/ota_controller.py"
    "$app_root/tests"
    "$app_root/tests/verify_state.py"
)
: > "$output/sealed-paths.txt"
for path in "${sealed_paths[@]}"; do
    test -e "$path"
    test ! -w "$path"
    stat -c '%U:%G %a %n' "$path" >> "$output/sealed-paths.txt"
done

test "$(id -u)" -ne 0
test "$(awk '/^Cap(Inh|Prm|Eff|Bnd|Amb):/ { if ($2 != "0000000000000000") bad=1 } END { print bad+0 }' "$output/capabilities.txt")" -eq 0
test ! -e /dev/kvm
test ! -e /dev/net/tun
test ! -e /dev/ttyACM0
test ! -e /dev/ttyUSB0
test ! -S /var/run/docker.sock
test "$(awk -F: 'NR > 2 && $1 !~ /lo/ { count++ } END { print count+0 }' /proc/net/dev)" -eq 0
grep -qx 'make' "$output/pid1-comm.txt"

python3 - "$output" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
payload = {
    "result": "pass",
    "uid": int(next(part.split("=")[1].split("(")[0]
                    for part in (root / "id.txt").read_text().split()
                    if part.startswith("uid="))),
    "all_capability_sets_zero": True,
    "network_interfaces": ["lo"],
    "kvm": False,
    "tap": False,
    "physical_serial": False,
    "docker_socket": False,
    "host_pid_namespace": False,
    "host_network_namespace": False,
    "sealed_inputs_read_only": True,
}
(root / "unprivileged-summary.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
