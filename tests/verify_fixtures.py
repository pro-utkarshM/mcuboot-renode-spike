#!/usr/bin/env python3
"""Verify generated firmware fixtures against the sealed build manifest."""

import argparse
import hashlib
import json
from pathlib import Path

EXPECTED_ZEPHYR_REVISION = "684c9e8f32e4373a21098559f748f06915f950c9"
DEFAULT_BOARD = "nrf52840dk/nrf52840"
MANIFEST = "firmware-builds.sha256"
SIGNED_FIXTURES = {
    "v1": "v1-signed.bin",
    "sealed_v1": "sealed-v1-signed.bin",
    "v2": "v2-signed.bin",
    "v2_auto_confirm": "v2-auto-confirm-signed.bin",
    "v2_negative_premature_confirm":
        "v2-negative-premature-confirm-signed.bin",
    "v2_negative_erase_after_confirm":
        "v2-negative-erase-after-confirm-signed.bin",
}
MCUBOOT_FIXTURES = (
    "v1-mcuboot.bin",
    "v2-mcuboot.bin",
    "v2-auto-confirm-mcuboot.bin",
    "v2-negative-premature-confirm-mcuboot.bin",
    "v2-negative-erase-after-confirm-mcuboot.bin",
)


def parse_manifest(path: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise SystemExit(f"{path}:{line_number}: malformed manifest line")
        key, value = line.split("=", 1)
        if not key or not value:
            raise SystemExit(f"{path}:{line_number}: empty manifest field")
        if key in payload:
            raise SystemExit(f"{path}:{line_number}: duplicate key {key}")
        payload[key] = value
    return payload


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"required fixture is missing: {path}")


def verify(fixtures: Path, expected_board: str) -> dict[str, object]:
    manifest_path = fixtures / MANIFEST
    require_file(manifest_path)
    manifest = parse_manifest(manifest_path)
    required_keys = {
        "zephyr_revision",
        "board",
        "mcuboot",
        *SIGNED_FIXTURES.keys(),
    }
    missing = sorted(required_keys - manifest.keys())
    if missing:
        raise SystemExit(
            f"fixture manifest is missing keys: {', '.join(missing)}")
    if manifest["zephyr_revision"] != EXPECTED_ZEPHYR_REVISION:
        raise SystemExit(
            "fixture manifest Zephyr revision does not match the pinned revision")
    if manifest["board"] != expected_board:
        raise SystemExit(
            "fixture manifest board does not match the expected board")

    hashes: dict[str, str] = {}
    for key, filename in SIGNED_FIXTURES.items():
        path = fixtures / filename
        require_file(path)
        actual = sha256(path)
        if manifest[key] != actual:
            raise SystemExit(f"fixture hash mismatch for {filename}")
        hashes[key] = actual

    if (fixtures / "v1-signed.bin").read_bytes() != (
            fixtures / "sealed-v1-signed.bin").read_bytes():
        raise SystemExit("sealed v1 fixture differs from v1-signed.bin")

    for filename in MCUBOOT_FIXTURES:
        path = fixtures / filename
        require_file(path)
        actual = sha256(path)
        if manifest["mcuboot"] != actual:
            raise SystemExit(f"MCUboot hash mismatch for {filename}")

    return {
        "result": "pass",
        "board": manifest["board"],
        "zephyr_revision": manifest["zephyr_revision"],
        "signed_fixture_hashes": hashes,
        "mcuboot_sha256": manifest["mcuboot"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--expected-board", default=DEFAULT_BOARD)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = verify(args.fixtures, args.expected_board)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
