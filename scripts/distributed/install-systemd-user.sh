#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

[[ ${1:-} == --config ]] || proof_die 'usage: install-systemd-user.sh --config FILE'
config=$(realpath "${2:?configuration path is required}")
proof_load_config "$config"
proof_require_command systemctl
proof_require_command systemd-analyze

unit_source="$PROOF_REPO/deployment/systemd/proof-worker@.service"
unit_dir="$HOME/.config/systemd/user"
config_dir="$HOME/.config/ota-proof"
dropin_dir="$unit_dir/proof-worker@.service.d"
[[ "$PROOF_ROOT" != *[$'\t\n ']* ]] \
    || proof_die 'PROOF_ROOT with whitespace is not supported by the systemd installer'
install -d -m 0700 "$unit_dir" "$config_dir" "$dropin_dir"
install -m 0644 "$unit_source" "$unit_dir/proof-worker@.service"
install -m 0600 "$config" "$config_dir/worker.env"
{
    printf '%s\n' '[Service]'
    printf '%s\n' 'ReadWritePaths='
    printf 'ReadWritePaths=%s\n' "$PROOF_ROOT"
} >"$dropin_dir/paths.conf"
chmod 0600 "$dropin_dir/paths.conf"
systemd-analyze --user verify "$unit_dir/proof-worker@.service"
systemctl --user daemon-reload
systemctl --user disable --now 'proof-worker@*.service' >/dev/null 2>&1 || true
for ((slot = 0; slot < PROOF_WORKER_JOBS; slot++)); do
    systemctl --user enable --now "proof-worker@$slot.service"
done

printf 'enabled %s worker slot(s)\n' "$PROOF_WORKER_JOBS"
printf 'for reboot persistence, an administrator must run: loginctl enable-linger %q\n' "$USER"
