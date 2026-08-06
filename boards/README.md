# Board selection

The spike uses the unmodified upstream Zephyr `nrf52840dk/nrf52840` board and
its fixed-partition map. The emulator-only platform replacement is isolated in
`renode/platform.repl`; no custom Zephyr board or application flash shim is
needed.
