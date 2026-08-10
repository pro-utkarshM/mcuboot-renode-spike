#!/usr/bin/env python3

import subprocess
import tempfile
import unittest
from pathlib import Path

from controller import ota_controller


IMAGE_LIST = """Images:
 image=0 slot=1
    version: 2.0.0
    hash: aabbccdd
"""


class FakeSession:
    def __init__(self, root: Path, fault_after: int, failure_mode: str,
                 emitted_fault_after: int | None = None):
        self.trace_path = root / "trace.log"
        self.fault_evidence = root / "fault-operation.txt"
        self.fault_snapshot = root / "fault-committed-flash.bin"
        self.process = subprocess.Popen(["sleep", "30"])
        self.fault_after = fault_after
        self.emitted_fault_after = emitted_fault_after or fault_after
        self.failure_mode = failure_mode

    def close(self):
        self.process.terminate()
        self.process.wait()

    def uart_offset(self):
        return 123

    def image_list(self):
        return IMAGE_LIST

    def run_mcumgr(self, *arguments, **kwargs):
        if arguments[:2] != ("image", "test"):
            return "ok"
        if self.failure_mode == "new-complete-fault":
            self.trace_path.write_text(
                f"op={self.emitted_fault_after} type=program "
                "address=0x00000000 length=4\n"
                f"fault=power-loss after_op={self.emitted_fault_after}\n")
            self.fault_evidence.write_text("complete\n")
            self.fault_snapshot.write_bytes(b"\xff" * ota_controller.FLASH_SIZE)
        elif self.failure_mode == "new-partial-fault":
            self.trace_path.write_text(
                f"fault=power-loss after_op={self.fault_after}\n")
        raise ota_controller.ControllerError("MCUmgr transport failed")


class RecoveryAwareStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image = self.root / "image.bin"
        self.image.write_bytes(b"image")

    def tearDown(self):
        self.temporary.cleanup()

    def run_stage(self, mode, fault_after=42, preexisting=False,
                  emitted_fault_after=None):
        session = FakeSession(
            self.root, fault_after, mode, emitted_fault_after)
        if preexisting:
            session.trace_path.write_text(
                f"fault=power-loss after_op={fault_after}\n")
            session.fault_evidence.write_text("complete\n")
            session.fault_snapshot.write_bytes(b"\xff" * ota_controller.FLASH_SIZE)
        try:
            return ota_controller.stage_update_recovery_aware(
                session, self.image, fault_after)
        finally:
            session.close()

    def test_new_complete_configured_fault_enters_recovery(self):
        image_hash, interrupted, offset = self.run_stage("new-complete-fault")
        self.assertIsNone(image_hash)
        self.assertTrue(interrupted)
        self.assertEqual(offset, 123)

    def test_unrelated_failure_without_fault_fails_closed(self):
        with self.assertRaises(ota_controller.ControllerError):
            self.run_stage("unrelated")

    def test_partial_fault_evidence_fails_closed(self):
        with self.assertRaises(ota_controller.ControllerError):
            self.run_stage("new-partial-fault")

    def test_preexisting_fault_does_not_mask_later_failure(self):
        with self.assertRaises(ota_controller.ControllerError):
            self.run_stage("unrelated", preexisting=True)

    def test_wrong_configured_operation_fails_closed(self):
        with self.assertRaises(ota_controller.ControllerError):
            self.run_stage(
                "new-complete-fault", fault_after=43,
                emitted_fault_after=42)


if __name__ == "__main__":
    unittest.main()
