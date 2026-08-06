#!/usr/bin/env bash
# Build the real sysbuild domains: MCUboot plus each signed application image.
# It intentionally does not create flash fixtures with dd, Python, or dummy
# data.  Every file copied to fixtures/ is an output asserted to exist after a
# successful CMake/Ninja sysbuild invocation.
set -euo pipefail

readonly APP_ROOT="${APP_ROOT:-/workspace/app}"
readonly ZEPHYR_WORKSPACE="${ZEPHYR_WORKSPACE:-/opt/zephyrproject}"
readonly BOARD="${BOARD:-nrf52840dk/nrf52840}"
readonly BUILD_ROOT="${BUILD_ROOT:-${APP_ROOT}/build}"
readonly FIXTURES_DIR="${FIXTURES_DIR:-${APP_ROOT}/fixtures}"
readonly KEY_FILE="${KEY_FILE:-${APP_ROOT}/keys/root-rsa-2048.pem}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${BUILD_ROOT}/.cache}"

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

require_file() {
    [[ -f "$1" ]] || die "required file is missing: $1"
}

copy_output() {
    local build_dir="$1"
    local relative="$2"
    local destination="$3"
    local source="${build_dir}/${relative}"
    require_file "$source"
    install -D -m 0644 "$source" "${FIXTURES_DIR}/${destination}"
}

build_sysbuild() {
    local name="$1"
    local app_dir="$2"
    local extra_conf="${3:-}"
    local build_dir="${BUILD_ROOT}/${name}"
    local app_domain
    local -a cmake_args=()

    app_domain="$(basename "$app_dir")"

    if [[ -n "$extra_conf" ]]; then
        require_file "${app_dir}/${extra_conf}"
        cmake_args+=("-DCONF_FILE=prj.conf;${extra_conf}")
    fi

    # Run west from its checked-out top directory.  The applications live in
    # /workspace/app so editable v2 sources remain separate from pinned Zephyr.
    (
        cd "$ZEPHYR_WORKSPACE"
        west build --pristine=always --board "$BOARD" --sysbuild \
            --build-dir "$build_dir" "$app_dir" -- "${cmake_args[@]}"
    )

    copy_output "$build_dir" "mcuboot/zephyr/zephyr.elf" "${name}-mcuboot.elf"
    copy_output "$build_dir" "mcuboot/zephyr/zephyr.bin" "${name}-mcuboot.bin"
    copy_output "$build_dir" "${app_domain}/zephyr/zephyr.elf" "${name}.elf"
    copy_output "$build_dir" "${app_domain}/zephyr/zephyr.signed.bin" "${name}-signed.bin"
}

require_file "${ZEPHYR_WORKSPACE}/.west/config"
require_file "${ZEPHYR_WORKSPACE}/zephyr/share/sysbuild/CMakeLists.txt"
require_file "${APP_ROOT}/firmware/v1/CMakeLists.txt"
require_file "${APP_ROOT}/firmware/v2/CMakeLists.txt"
require_file "$KEY_FILE"
command -v west >/dev/null || die "west is not installed"
command -v arm-zephyr-eabi-gcc >/dev/null || die "arm-zephyr-eabi-gcc is not installed"

mkdir -p "$BUILD_ROOT" "$FIXTURES_DIR" "$XDG_CACHE_HOME"
build_sysbuild v1 "${APP_ROOT}/firmware/v1"
build_sysbuild v2 "${APP_ROOT}/firmware/v2"
build_sysbuild v2-auto-confirm "${APP_ROOT}/firmware/v2" auto-confirm.conf
build_sysbuild v2-negative-premature-confirm "${APP_ROOT}/firmware/v2" negative-premature-confirm.conf
build_sysbuild v2-negative-erase-after-confirm "${APP_ROOT}/firmware/v2" negative-erase-after-confirm.conf

cmp -s "${FIXTURES_DIR}/v1-mcuboot.bin" "${FIXTURES_DIR}/v2-mcuboot.bin" \
    || die "MCUboot changed between the v1 and v2 sysbuild domains"

# A sealed v1 is the only immutable baseline binary.  The controller converts
# this and MCUboot's genuine sysbuild output into a full flash image; this
# script intentionally refuses to invent that controller-owned artifact.
install -D -m 0444 "${FIXTURES_DIR}/v1-signed.bin" "${FIXTURES_DIR}/sealed-v1-signed.bin"
printf '%s\n' \
    "zephyr_revision=684c9e8f32e4373a21098559f748f06915f950c9" \
    "board=${BOARD}" \
    "v1=$(sha256sum "${FIXTURES_DIR}/v1-signed.bin" | awk '{print $1}')" \
    "v2=$(sha256sum "${FIXTURES_DIR}/v2-signed.bin" | awk '{print $1}')" \
    > "${FIXTURES_DIR}/firmware-builds.sha256"
