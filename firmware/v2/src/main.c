/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * v2 provides both the correct post-durability confirmation path and a
 * deliberately premature-confirm negative variant for verifier sensitivity.
 */

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/dfu/mcuboot.h>
#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/printk.h>

#define SPIKE_STATE_MAGIC 0x5350494bU /* "SPIK" */
#define SPIKE_STATE_SCHEMA 1U
#define V2_DURABLE_MAGIC 0x56324452U /* "V2DR" */

struct spike_persistent_state {
	uint32_t magic;
	uint16_t schema;
	uint16_t reserved;
	uint32_t generation;
};

struct v2_durable_state {
	uint32_t magic;
	uint32_t migration_generation;
};

static struct spike_persistent_state persistent_state;
static struct v2_durable_state durable_state;
static bool persistent_state_loaded;
static bool durable_state_loaded;
static volatile uint32_t ram_boot_marker;

static int spike_settings_set(const char *name, size_t len_rd,
			      settings_read_cb read_cb, void *cb_arg)
{
	ssize_t read_len;

	if (strcmp(name, "state") == 0) {
		if (len_rd != sizeof(persistent_state)) {
			return -EINVAL;
		}
		read_len = read_cb(cb_arg, &persistent_state,
				   sizeof(persistent_state));
		if (read_len != sizeof(persistent_state) ||
		    persistent_state.magic != SPIKE_STATE_MAGIC ||
		    persistent_state.schema != SPIKE_STATE_SCHEMA) {
			return -EINVAL;
		}
		persistent_state_loaded = true;
		return 0;
	}

	if (strcmp(name, "v2_durable") == 0) {
		if (len_rd != sizeof(durable_state)) {
			return -EINVAL;
		}
		read_len = read_cb(cb_arg, &durable_state, sizeof(durable_state));
		if (read_len != sizeof(durable_state) ||
		    durable_state.magic != V2_DURABLE_MAGIC) {
			return -EINVAL;
		}
		durable_state_loaded = true;
		return 0;
	}

	/* Ignore future settings so a subsequent image can remain compatible. */
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(ota_spike, "spike", NULL, spike_settings_set,
			       NULL, NULL);

static int ensure_v1_state(void)
{
	int rc;

	if (persistent_state_loaded) {
		return 0;
	}

	persistent_state = (struct spike_persistent_state){
		.magic = SPIKE_STATE_MAGIC,
		.schema = SPIKE_STATE_SCHEMA,
		.generation = 1U,
	};
	rc = settings_save_one("spike/state", &persistent_state,
			       sizeof(persistent_state));
	if (rc == 0) {
		persistent_state_loaded = true;
	}
	return rc;
}

static int write_v2_durable_state(void)
{
	int rc;

	if (durable_state_loaded) {
		/* Do not emit the continuation sentinel on a post-reset reload. */
		printk("DURABLE_STATE=already-present\n");
		return 0;
	}

	durable_state = (struct v2_durable_state){
		.magic = V2_DURABLE_MAGIC,
		.migration_generation = 2U,
	};
	/*
	 * Gives the controller a clean observation point before the synchronous
	 * settings write. The flash model does not consume this marker; it observes
	 * only NVMC traffic. DURABLE_WRITE_SENTINEL remains the first statement
	 * after settings_save_one() returns.
	 */
	printk("DURABLE_WRITE_ARMED=1\n");
	k_sleep(K_MSEC(250));
	rc = settings_save_one("spike/v2_durable", &durable_state,
			       sizeof(durable_state));
	if (rc != 0) {
		return rc;
	}

	/* Keep this as the statement immediately after the synchronous NVS write. */
	printk("DURABLE_WRITE_SENTINEL=1\n");
	durable_state_loaded = true;
	return 0;
}

static void report_confirm_result(int rc)
{
	printk("IMAGE_CONFIRMATION=%s:rc=%d\n", rc == 0 ? "complete" : "error", rc);
}

int main(void)
{
	int rc;

	/* A machine reset clears .bss, so every fresh boot must report one. */
	ram_boot_marker++;
	printk("FIRMWARE_VERSION=2.0.0\n");
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

	rc = ensure_v1_state();
	if (rc != 0) {
		printk("PERSISTENT_SETTING=error:save-v1:%d\n", rc);
		return rc;
	}
	printk("PERSISTENT_SETTING=%s:generation=%u\n",
	       persistent_state_loaded ? "loaded" : "initialized",
	       persistent_state.generation);

#if defined(CONFIG_SPIKE_V2_NEGATIVE_PREMATURE_CONFIRM)
	/* Deliberately wrong: the fault model can cut before write_v2_durable_state(). */
	printk("NEGATIVE_PREMATURE_CONFIRM=1\n");
	rc = boot_write_img_confirmed();
	report_confirm_result(rc);
	if (rc != 0) {
		return rc;
	}
	/* Deliberately never establish the state whose durability was required. */
	printk("NEGATIVE_DURABLE_STATE=skipped\n");
	return 0;
#endif

	rc = write_v2_durable_state();
	if (rc != 0) {
		printk("PERSISTENT_SETTING=error:save-v2:%d\n", rc);
		return rc;
	}

#if defined(CONFIG_SPIKE_V2_AUTO_CONFIRM_AFTER_DURABLE)
	rc = boot_write_img_confirmed();
	report_confirm_result(rc);
	if (rc != 0) {
		return rc;
	}
#elif defined(CONFIG_SPIKE_V2_NEGATIVE_ERASE_AFTER_CONFIRM)
	rc = boot_write_img_confirmed();
	report_confirm_result(rc);
	if (rc != 0) {
		return rc;
	}
	rc = settings_delete("spike/v2_durable");
	printk("NEGATIVE_DURABLE_STATE=deleted:rc=%d\n", rc);
	if (rc != 0) {
		return rc;
	}
#elif defined(CONFIG_SPIKE_V2_UNCONFIRMED)
	printk("IMAGE_CONFIRMATION=unconfirmed\n");
#endif

	return 0;
}
