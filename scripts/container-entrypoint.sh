#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
    printf '%s\n' 'error: refusing to run the proof container as root' >&2
    exit 126
fi

export IN_CONTAINER=1
export HOME="${HOME:-/home/spike}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp/spike-cache}"
mkdir -p "$XDG_CACHE_HOME"

if [[ "$#" -eq 0 ]]; then
    set -- make proof
fi

exec "$@"
