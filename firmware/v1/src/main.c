/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Immutable deployed image for the MCUboot/MCUmgr power-loss spike.
 */

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/printk.h>

#define SPIKE_STATE_MAGIC 0x5350494bU /* "SPIK" */
#define SPIKE_STATE_SCHEMA 1U

struct spike_persistent_state {
	uint32_t magic;
	uint16_t schema;
	uint16_t reserved;
	uint32_t generation;
};

static struct spike_persistent_state persistent_state;
static bool persistent_state_loaded;
static volatile uint32_t ram_boot_marker;

static int spike_settings_set(const char *name, size_t len_rd,
			      settings_read_cb read_cb, void *cb_arg)
{
	ssize_t read_len;

	/* v1 must tolerate v2's forward-compatible settings after a revert. */
	if (strcmp(name, "state") != 0) {
		return 0;
	}
	if (len_rd != sizeof(persistent_state)) {
		return -EINVAL;
	}

	read_len = read_cb(cb_arg, &persistent_state, sizeof(persistent_state));
	if (read_len != sizeof(persistent_state) ||
	    persistent_state.magic != SPIKE_STATE_MAGIC ||
	    persistent_state.schema != SPIKE_STATE_SCHEMA) {
		return -EINVAL;
	}

	persistent_state_loaded = true;
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(ota_spike, "spike", NULL, spike_settings_set,
			       NULL, NULL);

int main(void)
{
	int rc;

	/* A machine reset clears .bss, so every fresh boot must report one. */
	ram_boot_marker++;
	printk("FIRMWARE_VERSION=1.0.0\n");
	printk("RAM_BOOT_MARKER_RESET=%u\n", ram_boot_marker);

	rc = settings_subsys_init();
	if (rc != 0) {
		printk("PERSISTENT_SETTING=error:init:%d\n", rc);
		return rc;
	}

	rc = settings_load();
	if (rc != 0) {
		printk("PERSISTENT_SETTING=error:load:%d\n", rc);
		return rc;
	}

	if (!persistent_state_loaded) {
		persistent_state = (struct spike_persistent_state){
			.magic = SPIKE_STATE_MAGIC,
			.schema = SPIKE_STATE_SCHEMA,
			.generation = 1U,
		};
		rc = settings_save_one("spike/state", &persistent_state,
				       sizeof(persistent_state));
		if (rc != 0) {
			printk("PERSISTENT_SETTING=error:save:%d\n", rc);
			return rc;
		}
		printk("PERSISTENT_SETTING=initialized:generation=1\n");
	} else {
		printk("PERSISTENT_SETTING=loaded:generation=%u\n",
		       persistent_state.generation);
	}

	return 0;
}
