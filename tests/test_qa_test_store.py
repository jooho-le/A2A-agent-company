"""Exact private QA input preservation and immutable receipt bindings."""

from dataclasses import replace
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from agents.runtime.qa_test_store import QATestInputStore, QATestInputStoreError, _plain
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.domain.states import AgentRole, WorkflowStatus
import test_mcp_unit_store as store_fixture


class QATestInputStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = store_fixture.UnitTestOutputStoreTests("test_constructor_is_inert")
        self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.step = self.fixture.qa_context()
        self.store = QATestInputStore(self.fixture.repository)
        self.inventory = UnitTestOutputStore._inputs_payload(self.fixture.inputs, self.fixture.scope)

    def stage(self, **changes):
        values = dict(workflow_step_id=self.step.workflow_step_id, source_artifact_id=self.fixture.source.artifact_id,
            tool_name="run_unit_tests", selector=self.fixture.scope.name, captured=self.fixture.inputs, inputs=self.inventory)
        values.update(changes)
        return self.store.stage(self.fixture.binding, **values)

    def receipt(self):
        return self.fixture.store.publish(self.fixture.binding, self.fixture.source, self.fixture.result,
            profile=self.fixture.profile, scope=self.fixture.scope, inputs=self.fixture.inputs, report=self.fixture.report)

    def test_constructor_and_repr_inert(self):
        with patch.object(self.fixture.repository, "_connection", side_effect=AssertionError("inert")), \
                patch.object(self.fixture.repository, "_transaction", side_effect=AssertionError("inert")):
            store = QATestInputStore(self.fixture.repository)
        self.assertEqual(repr(store), "QATestInputStore()")

    def test_exact_bytes_hash_inventory_round_trip(self):
        record = self.stage()
        reloaded = QATestInputStore(self.fixture.repository).get(self.fixture.binding, record.capture_id)
        self.assertEqual(record, reloaded)
        self.assertEqual(dict(record.files), dict(self.fixture.inputs.files))
        self.assertEqual(dict(record.inputs), self.inventory)
        self.assertEqual(record.workflow_step_id, self.step.workflow_step_id)
        self.assertEqual(record.source_artifact_id, self.fixture.source.artifact_id)
        self.assertNotIn("protected criteria", repr(record))
        with self.assertRaises(TypeError):
            record.files["tests/test_signup.py"] = b"modified"

    def test_wrong_selector_or_input_hash_rejected(self):
        for changes in ({"selector": "../path"}, {"tool_name": "run_build"},
                        {"inputs": {**self.inventory, "inputsSha256": "a" * 64}}):
            with self.subTest(changes=changes), self.assertRaises(QATestInputStoreError):
                self.stage(**changes)

    def test_unknown_source_and_other_workflow_step_rejected(self):
        for changes in ({"source_artifact_id": uuid4()}, {"workflow_step_id": self.fixture.step.workflow_step_id}):
            with self.subTest(changes=changes), self.assertRaises(QATestInputStoreError):
                self.stage(**changes)

    def test_wrong_role_and_run_cannot_read(self):
        record = self.stage()
        bindings = (MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
                              run_id=record.run_id, workspace_id=record.workspace_id),
                    MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA,
                              run_id=uuid4(), workspace_id=record.workspace_id))
        for binding in bindings:
            with self.subTest(binding=binding), self.assertRaises(QATestInputStoreError):
                self.store.get(binding, record.capture_id)

    def test_aborted_run_does_not_accept_new_capture(self):
        self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        with self.assertRaises(QATestInputStoreError):
            self.stage()

    def test_historical_capture_survives_completion_and_scratch_changes(self):
        record = self.stage()
        self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        self.assertEqual(self.store.get(self.fixture.binding, record.capture_id), record)

    def test_files_captures_are_not_replaceable_updatable_or_deletable(self):
        record = self.stage()
        for statement, values in (
            ("UPDATE qa_test_input_files SET content=? WHERE capture_id=?", (b"new", str(record.capture_id))),
            ("DELETE FROM qa_test_input_files WHERE capture_id=?", (str(record.capture_id),)),
            ("DELETE FROM qa_test_input_captures WHERE capture_id=?", (str(record.capture_id),)),
            ("INSERT OR REPLACE INTO qa_test_input_files VALUES(?,?,?,?)", (
                str(record.capture_id), "tests/test_signup.py", b"new", sha256(b"new").hexdigest())),
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with self.fixture.repository._transaction() as connection:
                    connection.execute(statement, values)
        self.assertEqual(self.store.get(self.fixture.binding, record.capture_id), record)

    def test_capture_id_replacement_is_denied(self):
        record = self.stage()
        with patch("agents.runtime.qa_test_store.uuid4", return_value=record.capture_id):
            with self.assertRaises(QATestInputStoreError):
                self.stage()

    def test_actual_receipt_binds_exact_inventory_after_deepfreeze(self):
        record, receipt = self.stage(), self.receipt()
        self.assertIsInstance(receipt.inputs["files"], tuple)
        self.assertEqual(_plain(receipt.inputs), dict(record.inputs))
        self.assertEqual(self.store.bind_receipt(self.fixture.binding, record.capture_id, receipt), record)
        with self.fixture.repository._connection() as connection:
            row = connection.execute("SELECT execution_manifest_id FROM qa_test_input_receipts WHERE capture_id=?",
                                     (str(record.capture_id),)).fetchone()
        self.assertEqual(row[0], str(receipt.execution_manifest_id))

    def test_receipt_cannot_bind_twice_or_to_another_capture(self):
        record, receipt = self.stage(), self.receipt()
        self.store.bind_receipt(self.fixture.binding, record.capture_id, receipt)
        for capture in (record, self.stage()):
            with self.subTest(capture=capture), self.assertRaises(QATestInputStoreError):
                self.store.bind_receipt(self.fixture.binding, capture.capture_id, receipt)

    def test_receipt_inventory_and_selector_are_verified(self):
        record, receipt = self.stage(), self.receipt()
        for changed in (replace(receipt, profile_name="other-selector"),
                        replace(receipt, inputs={**dict(receipt.inputs), "inputsSha256": "e" * 64}),
                        replace(receipt, source_artifact_id=uuid4())):
            with self.subTest(receipt=changed), self.assertRaises(QATestInputStoreError):
                self.store.bind_receipt(self.fixture.binding, record.capture_id, changed)

    def test_corrupt_private_file_bytes_cannot_reload(self):
        record = self.stage()
        with self.fixture.repository._transaction() as connection:
            connection.execute("DROP TRIGGER qa_test_input_files_no_update")
            connection.execute("UPDATE qa_test_input_files SET content=? WHERE capture_id=? AND path='tests/test_signup.py'",
                               (b"corrupt", str(record.capture_id)))
        with self.assertRaises(QATestInputStoreError):
            self.store.get(self.fixture.binding, record.capture_id)

    def test_corrupt_metadata_is_not_accepted(self):
        record = self.stage()
        with self.fixture.repository._transaction() as connection:
            connection.execute("DROP TRIGGER qa_test_input_captures_no_update")
            connection.execute("UPDATE qa_test_input_captures SET metadata_json=? WHERE capture_id=?",
                               (json.dumps({"changed": True}), str(record.capture_id)))
        with self.assertRaises(QATestInputStoreError):
            self.store.get(self.fixture.binding, record.capture_id)


if __name__ == "__main__":
    unittest.main()
