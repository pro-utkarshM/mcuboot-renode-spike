# OTA spike v2 modes

The default build is deliberately unconfirmed and therefore exercises MCUboot
revert after a trial boot.  Add `auto-confirm.conf` to `CONF_FILE` to confirm
only after `settings_save_one("spike/v2_durable", ...)` returns.  Add
`negative-premature-confirm.conf` to build the intentionally unsafe verifier
variant, which confirms before that state is durable.
`negative-erase-after-confirm.conf` builds the second unsafe variant, which
deletes the required durable state after confirming the image.

`DURABLE_WRITE_SENTINEL=1` is emitted only by the statement immediately
following the synchronous NVS save.  It is evidence that guest execution
continued; a later boot reports `DURABLE_STATE=already-present` instead, so a
reset cannot be mistaken for continuation.  The Renode fault model must never
use UART output to decide whether to reset.
