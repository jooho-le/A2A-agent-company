"""Private proof ledger with actual temporary Source/SQLite, never execution.

All semantic outcomes and evidence hashes below are inert test declarations,
not vulnerability confirmation or acceptance results for a real product.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

from agents.platform.security_proof_store import (
    SecurityProofReceipt, SecurityProofReceiptError, SecurityProofReceiptStore,
)
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import AgentRole, SCENARIO_REGISTRY, WorkflowStep, WorkflowStepStatus, WorkflowStatus
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.states import FinalVerdict
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
import test_mcp_security_store as source_fixture


_TABLE = "security_semantic_proof_receipts"
_SECRET = "private-proof-test-body-never-a-credential"


class SecurityProofStoreTests(unittest.TestCase):
    def setUp(self):
        fixture = source_fixture.SecurityScanOutputStoreTests("test_constructor_is_inert_and_safe")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.repository, self.directory = fixture.repository, fixture.directory
        self.ids = SCENARIO_REGISTRY[fixture.run.scenario_id].requirement_ids_for(RequirementValidator.SECURITY)
        self.step = WorkflowStep.model_validate({**fixture.step.model_dump(), "requirement_ids": list(self.ids)})
        fixture.mutate_step(self.step)
        self.source = fixture.source
        self.register_source(self.source)
        self.store = SecurityProofReceiptStore(self.repository)
        self.evidence = b'{"independentInertFixture":"not product evidence"}'
        self.receipt = SecurityProofReceipt(
            receipt_id=uuid4(), request_id=uuid4(), run_id=fixture.run.run_id,
            workspace_id=fixture.run.workspace_id, workflow_step_id=self.step.workflow_step_id,
            source_artifact_id=self.source.artifact_id, code_version=1, attempt=0,
            snapshot_sha256=self.source.snapshot_sha256, approval_sha256="a" * 64,
            request_sha256="b" * 64, assessment_sha256="c" * 64,
            evidence_sha256=sha256(self.evidence).hexdigest(),
            requirement_outcomes=((self.ids[0], "UNVERIFIED"),),
            finding_dispositions=((str(uuid4()), "SUSPECTED"),),
            evidence_refs=(f"artifact://{self.source.artifact_id}/source.tar#path=main.py&startLine=1&endLine=1",
                           f"artifact://{uuid4()}/tool-execution-evidence.json"),
        )

    def register_source(self, source):
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO project_artifacts VALUES(?,?,?,?,?)", (
                str(source.artifact_id), str(source.run_id), "SOURCE", source.artifact_version,
                source.model_dump_json()))

    def tables(self):
        with self.repository._connection() as connection:
            return tuple(row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))

    def records(self):
        with self.repository._connection() as connection:
            return tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {_TABLE} ORDER BY receipt_id"))

    def rejected(self, operation, *arguments, **kwargs):
        with self.assertRaises(SecurityProofReceiptError) as caught:
            operation(*arguments, **kwargs)
        self.assertEqual(str(caught.exception), "SECURITY_PROOF_RECEIPT_INVALID")
        self.assertEqual(caught.exception.code, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(_SECRET, str(caught.exception))
        self.assertNotIn(str(self.directory), str(caught.exception))

    def mutate_step(self, **changes):
        original = next(step for step in self.repository.list_steps(self.receipt.run_id)
                        if step.workflow_step_id == self.step.workflow_step_id)
        updated = WorkflowStep.model_validate({**original.model_dump(), **changes})
        self.fixture.mutate_step(updated)
        return updated

    def corrupt(self, **columns):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER security_proof_receipts_no_update")
            for name, value in columns.items():
                self.assertIn(name, {"metadata_json", "metadata_sha256", "run_id", "workspace_id",
                                    "workflow_step_id", "source_artifact_id", "code_version", "attempt"})
                connection.execute(f"UPDATE {_TABLE} SET {name}=? WHERE receipt_id=?",
                    (value, str(self.receipt.receipt_id)))

    def test_receipt_is_frozen_private_and_uses_opaque_registry_reference(self):
        with self.assertRaises(FrozenInstanceError):
            self.receipt.attempt = 2
        self.assertEqual(repr(self.receipt), "SecurityProofReceipt()")
        self.assertEqual(self.receipt.receipt_ref,
            f"artifact://{self.receipt.receipt_id}/security-semantic-proof.json")
        self.assertNotIn(str(self.directory), repr(self.receipt))

    def test_constructor_requires_exact_uuid4_for_every_identity(self):
        for field in ("receipt_id", "request_id", "run_id", "workspace_id", "workflow_step_id", "source_artifact_id"):
            for value in (str(uuid4()), uuid1(), None, True):
                with self.subTest(field=field, type=type(value).__name__):
                    self.rejected(replace, self.receipt, **{field: value})

    def test_all_hashes_are_strict_lowercase_sha256_hex(self):
        for field in ("snapshot_sha256", "approval_sha256", "request_sha256", "assessment_sha256", "evidence_sha256"):
            for value in ("A" * 64, "g" * 64, "a" * 63, "sha256:" + "a" * 64, b"a" * 64, True):
                with self.subTest(field=field, type=type(value).__name__):
                    self.rejected(replace, self.receipt, **{field: value})

    def test_code_version_and_attempt_are_noncoercing_integers(self):
        for field, values in (("code_version", (True, False, 1.0, "1", 0, -1)),
                              ("attempt", (True, False, 0.0, "0", -1))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.rejected(replace, self.receipt, **{field: value})

    def test_requirement_pairs_are_immutable_bounded_unique_and_closed_outcomes(self):
        for value in ([(self.ids[0], "PASS")], ([self.ids[0], "PASS"],), ((str(self.ids[0]), "PASS"),),
                      ((uuid1(), "PASS"),), ((self.ids[0], "CONFIRMED"),),
                      ((self.ids[0], "PASS"), (self.ids[0], "FAIL")),
                      tuple((uuid4(), "PASS") for _ in range(257))):
            with self.subTest(pair_count=len(value)):
                self.rejected(replace, self.receipt, requirement_outcomes=value)

    def test_finding_pairs_accept_existing_dispositions_not_requirement_outcomes(self):
        identity = str(uuid4())
        for outcome in ("CONFIRMED", "FALSE_POSITIVE", "SUSPECTED", "UNVERIFIED"):
            self.assertEqual(replace(self.receipt, finding_dispositions=((identity, outcome),)).finding_dispositions,
                             ((identity, outcome),))
        for value in ([(identity, "CONFIRMED")], ([identity, "CONFIRMED"],), ((identity, "PASS"),),
                      ((identity, "FAIL"),), (("", "UNVERIFIED"),), (("/Host/path", "UNVERIFIED"),),
                      (("raw prose " + _SECRET, "UNVERIFIED"),),
                      ((identity, "CONFIRMED"), (identity, "SUSPECTED")),
                      tuple((str(uuid4()), "UNVERIFIED") for _ in range(1001))):
            with self.subTest(pair_count=len(value)):
                self.rejected(replace, self.receipt, finding_dispositions=value)

    def test_evidence_accepts_safe_encoded_source_anchors_and_existing_tool_refs(self):
        authority = self.source.artifact_id
        references = (
            f"artifact://{authority}/source.tar#path=src/auth.py",
            f"artifact://{authority}/source.tar#path=src%2Fauth.py",
            f"artifact://{authority}/source.tar#path=%ED%95%9C%EA%B8%80.py&startLine=1&endLine=2",
            f"artifact://{uuid4()}/security-scan-report.json",
            f"artifact://{uuid4()}/tool-attempt.json",
        )
        self.assertEqual(replace(self.receipt, evidence_refs=references).evidence_refs, references)

    def test_evidence_rejects_host_paths_secret_paths_traversal_queries_and_raw_prose(self):
        authority = self.source.artifact_id
        for reference in (
            "/Host/private/file", "file:///Host/file", "https://example.invalid/evidence", "private raw " + _SECRET,
            f"artifact://{uuid1()}/evidence.json", "artifact://bad/evidence.json",
            f"artifact://{authority}/../secret", f"artifact://{authority}/.env",
            f"artifact://{authority}/report.json?token=private", f"artifact://{authority}/report.json#path=main.py",
            f"artifact://{authority}/source.tar#path=../private", f"artifact://{authority}/source.tar#path=%2E%2E%2Fprivate",
            f"artifact://{authority}/source.tar#path=.env", f"artifact://{authority}/source.tar#path=credentials.json",
            f"artifact://{authority}/source.tar#path=%2FHost%2Fprivate", f"artifact://{authority}/source.tar#path=main.py&path=other.py",
            f"artifact://{authority}/source.tar#path=main.py&startLine=0&endLine=1",
            f"artifact://{authority}/source.tar#path=main.py&startLine=2&endLine=1",
            f"artifact://{authority}/source.tar#path=main.py&startLine=1", f"artifact://{authority}/source.tar#path=main.py&unknown=x",
            f"artifact://{authority}/source.tar#path=main.py%0A", f"artifact://{authority}/source.tar#path=%FF",
        ):
            with self.subTest(reference_prefix=reference[:40]):
                self.rejected(replace, self.receipt, evidence_refs=(reference,))

    def test_evidence_tuple_uniqueness_count_and_aggregate_byte_bounds(self):
        reference = self.receipt.evidence_refs[0]
        for references in ([reference], (reference, reference), tuple(
                f"artifact://{uuid4()}/evidence.json" for _ in range(257)), (
                f"artifact://{uuid4()}/" + "a" * 4096,), tuple(
                f"artifact://{uuid4()}/" + "a" * 1200 for _ in range(256))):
            with self.subTest(count=len(references)):
                self.rejected(replace, self.receipt, evidence_refs=references)

    def test_store_constructor_is_inert_and_private(self):
        before = self.tables()
        with patch.object(self.repository, "_connection", side_effect=AssertionError("no read")), \
                patch.object(self.repository, "_transaction", side_effect=AssertionError("no write")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("no read")):
            self.assertEqual(repr(SecurityProofReceiptStore(self.repository)), "SecurityProofReceiptStore()")
        self.assertEqual(self.tables(), before)
        self.assertNotIn(_TABLE, before)
        self.rejected(SecurityProofReceiptStore, object())

    def test_publish_and_read_bind_registered_scope_without_mutating_public_records(self):
        run = self.repository.get_run(self.receipt.run_id)
        steps = self.repository.list_steps(self.receipt.run_id)
        source = self.repository.list_project_artifacts(self.receipt.run_id)
        events = self.repository.list_events(self.receipt.run_id, limit=1000, offset=0)
        config = self.repository.get_run_configuration(self.receipt.run_id)
        published = self.store.publish(self.receipt)
        self.assertEqual(published, self.receipt)
        self.assertIsNot(published, self.receipt)
        self.assertEqual(self.store.get(self.receipt.run_id, self.receipt.receipt_id), self.receipt)
        self.assertEqual(self.repository.get_run(self.receipt.run_id), run)
        self.assertEqual(self.repository.list_steps(self.receipt.run_id), steps)
        self.assertEqual(self.repository.list_project_artifacts(self.receipt.run_id), source)
        self.assertEqual(self.repository.list_events(self.receipt.run_id, limit=1000, offset=0), events)
        self.assertEqual(self.repository.get_run_configuration(self.receipt.run_id), config)
        self.assertEqual(len(self.records()), 1)

    def test_publish_revalidates_frozen_record_after_privileged_object_mutation(self):
        object.__setattr__(self.receipt, "attempt", False)
        self.rejected(self.store.publish, self.receipt)
        self.assertNotIn(_TABLE, self.tables())
        self.rejected(self.store.publish, object())

    def test_private_json_persists_hashes_refs_not_source_or_raw_independent_evidence(self):
        self.store.publish(self.receipt)
        raw = self.records()[0][-2]
        self.assertIn(self.receipt.evidence_sha256, raw)
        self.assertIn(self.receipt.evidence_refs[0], raw)
        self.assertNotIn(self.evidence.decode(), raw)
        self.assertNotIn("Do not execute this Source", raw)
        self.assertNotIn(str(self.directory), raw)
        self.assertNotIn(_SECRET, raw)

    def test_duplicate_publication_is_idempotent_only_while_scope_is_current(self):
        self.store.publish(self.receipt)
        with patch.object(SecurityProofReceiptStore, "_schema", side_effect=AssertionError("no duplicate schema write")):
            self.assertEqual(self.store.publish(self.receipt), self.receipt)
        self.assertEqual(len(self.records()), 1)
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.rejected(self.store.publish, self.receipt)
        self.assertEqual(self.store.get(self.receipt.run_id, self.receipt.receipt_id), self.receipt)

    def test_request_nonce_and_receipt_id_cannot_be_reused_for_different_result(self):
        self.store.publish(self.receipt)
        variants = (replace(self.receipt, receipt_id=uuid4()), replace(self.receipt, request_id=uuid4()),
                    replace(self.receipt, evidence_sha256="f" * 64),
                    replace(self.receipt, requirement_outcomes=((self.ids[0], "PASS"),)))
        for receipt in variants:
            with self.subTest(receipt_id=receipt.receipt_id):
                self.rejected(self.store.publish, receipt)
        self.assertEqual(self.store.get(self.receipt.run_id, self.receipt.receipt_id), self.receipt)

    def test_missing_registered_id_rejects_before_receipt_schema_creation(self):
        for field in ("run_id", "workspace_id", "workflow_step_id", "source_artifact_id"):
            with self.subTest(field=field):
                self.rejected(self.store.publish, replace(self.receipt, **{field: uuid4()}))
                self.assertNotIn(_TABLE, self.tables())

    def test_unregistered_source_and_nonsecurity_step_cannot_publish(self):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER project_artifacts_no_delete")
            connection.execute("DELETE FROM project_artifacts WHERE artifact_id=?", (str(self.source.artifact_id),))
        self.rejected(self.store.publish, self.receipt)
        self.assertNotIn(_TABLE, self.tables())

    def test_wrong_role_attempt_code_version_and_source_only_inputs_are_rejected(self):
        for change in ({"agent_role": AgentRole.QA}, {"attempt": 1}, {"code_version": 2},
                       {"input_artifact_ids": []}, {"input_artifact_ids": [self.source.artifact_id, uuid4()]},
                       {"status": WorkflowStepStatus.PENDING}):
            original = self.step
            with self.subTest(fields=tuple(change)):
                self.mutate_step(**change)
                self.rejected(self.store.publish, self.receipt)
                self.fixture.mutate_step(original)
        self.assertNotIn(_TABLE, self.tables())

    def test_exact_current_validation_fix_cycle_is_required(self):
        for change in ({"status": WorkflowStatus.IMPLEMENTING}, {"status": WorkflowStatus.FIXING},
                       {"status": WorkflowStatus.REVALIDATING}, {"code_version": 2},
                       {"fix_attempt": 1}, {"status": WorkflowStatus.REVALIDATING, "code_version": 2, "fix_attempt": 1}):
            with self.subTest(fields=tuple(change)):
                self.fixture.mutate_run(**change)
                self.rejected(self.store.publish, self.receipt)
                self.fixture.mutate_run(status=WorkflowStatus.VALIDATING, code_version=1, fix_attempt=0)
        self.assertNotIn(_TABLE, self.tables())

    def test_multiple_running_security_steps_cannot_publish_ambiguous_scope(self):
        extra = WorkflowStep(run_id=self.receipt.run_id, agent_role=AgentRole.SECURITY,
            status=WorkflowStepStatus.RUNNING, code_version=1, requirement_ids=list(self.ids),
            input_artifact_ids=[self.source.artifact_id])
        self.fixture.insert_step(extra)
        self.rejected(self.store.publish, self.receipt)
        self.assertNotIn(_TABLE, self.tables())

    def test_persisted_workflow_counters_cannot_be_coerced_into_a_valid_subject(self):
        for table, identity_column, identity, field, value in (
            ("workflow_runs", "run_id", self.receipt.run_id, "fix_attempt", False),
            ("workflow_runs", "run_id", self.receipt.run_id, "code_version", 1.0),
            ("workflow_steps", "workflow_step_id", self.step.workflow_step_id, "attempt", False),
            ("workflow_steps", "workflow_step_id", self.step.workflow_step_id, "code_version", 1.0),
        ):
            with self.subTest(table=table, field=field):
                with self.repository._transaction() as connection:
                    original = connection.execute(f"SELECT payload_json FROM {table} WHERE {identity_column}=?",
                        (str(identity),)).fetchone()[0]
                    data = json.loads(original)
                    data[field] = value
                    connection.execute(f"UPDATE {table} SET payload_json=? WHERE {identity_column}=?",
                        (json.dumps(data), str(identity)))
                self.rejected(self.store.publish, self.receipt)
                with self.repository._transaction() as connection:
                    connection.execute(f"UPDATE {table} SET payload_json=? WHERE {identity_column}=?",
                        (original, str(identity)))
        self.assertNotIn(_TABLE, self.tables())

    def test_requirement_outcomes_must_belong_to_both_step_and_frozen_security_scenario(self):
        self.rejected(self.store.publish, replace(self.receipt, requirement_outcomes=((uuid4(), "PASS"),)))
        nonsecurity = next(identifier for identifier in self.source.requirement_ids if identifier not in self.ids)
        self.mutate_step(requirement_ids=[nonsecurity])
        self.rejected(self.store.publish, replace(self.receipt, requirement_outcomes=((nonsecurity, "PASS"),)))
        self.fixture.mutate_step(self.step)
        self.mutate_step(requirement_ids=[self.ids[1]])
        self.rejected(self.store.publish, self.receipt)

    def test_snapshot_hash_must_match_registered_frozen_source(self):
        self.rejected(self.store.publish, replace(self.receipt, snapshot_sha256="f" * 64))
        self.assertNotIn(_TABLE, self.tables())

    def test_terminal_or_later_revision_reads_are_historical_not_new_admission(self):
        self.store.publish(self.receipt)
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED, attempt=1)
        self.fixture.mutate_run(status=WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS,
                                code_version=2, fix_attempt=1)
        self.assertEqual(self.store.get(self.receipt.run_id, self.receipt.receipt_id), self.receipt)
        self.rejected(self.store.publish, self.receipt)

    def test_reopened_store_retains_exact_receipt(self):
        self.store.publish(self.receipt)
        reopened = SecurityProofReceiptStore(SQLiteWorkflowRepository(self.repository.database_path))
        self.assertEqual(reopened.get(self.receipt.run_id, self.receipt.receipt_id), self.receipt)
        self.assertEqual(reopened.publish(self.receipt), self.receipt)

    def test_read_missing_database_or_table_never_creates_them(self):
        before = self.tables()
        self.rejected(self.store.get, self.receipt.run_id, self.receipt.receipt_id)
        self.assertEqual(self.tables(), before)
        missing = self.directory / "not-created" / "missing.sqlite3"
        with patch.object(self.repository, "_database_path", missing):
            self.rejected(self.store.get, self.receipt.run_id, self.receipt.receipt_id)
        self.assertFalse(missing.parent.exists())

    def test_read_uses_readonly_connection_not_mutating_repository_helpers(self):
        self.store.publish(self.receipt)
        before = self.records()
        with patch.object(self.repository, "_connection", side_effect=AssertionError("no creating open")), \
                patch.object(self.repository, "_transaction", side_effect=AssertionError("no mutation")):
            self.assertEqual(self.store.get(self.receipt.run_id, self.receipt.receipt_id), self.receipt)
        self.assertEqual(self.records(), before)

    def test_read_rejects_unknown_and_cross_run_ids_and_nonuuid_parameters(self):
        self.store.publish(self.receipt)
        for run_id, receipt_id in ((uuid4(), self.receipt.receipt_id), (self.receipt.run_id, uuid4()),
                                   (str(self.receipt.run_id), self.receipt.receipt_id),
                                   (self.receipt.run_id, uuid1())):
            with self.subTest(run_id_type=type(run_id).__name__):
                self.rejected(self.store.get, run_id, receipt_id)

    def test_update_delete_and_replace_are_blocked_by_sqlite(self):
        self.store.publish(self.receipt)
        original = self.records()
        for query, parameters in (
            (f"UPDATE {_TABLE} SET metadata_sha256=? WHERE receipt_id=?", ("f" * 64, str(self.receipt.receipt_id))),
            (f"DELETE FROM {_TABLE} WHERE receipt_id=?", (str(self.receipt.receipt_id),)),
            (f"INSERT OR REPLACE INTO {_TABLE} SELECT * FROM {_TABLE}", ()),
        ):
            with self.subTest(operation=query.split()[0]):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.repository._transaction() as connection:
                        connection.execute(query, parameters)
        self.assertEqual(self.records(), original)

    def test_metadata_hash_and_subject_columns_are_rechecked(self):
        self.store.publish(self.receipt)
        self.corrupt(metadata_sha256="f" * 64)
        self.rejected(self.store.get, self.receipt.run_id, self.receipt.receipt_id)
        self.rejected(self.store.publish, self.receipt)

    def test_modified_metadata_with_recomputed_hash_cannot_change_subject(self):
        self.store.publish(self.receipt)
        raw = self.records()[0][-2]
        data = json.loads(raw)
        data["snapshot_sha256"] = "f" * 64
        changed = json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        self.corrupt(metadata_json=changed, metadata_sha256=sha256(changed.encode()).hexdigest())
        self.rejected(self.store.get, self.receipt.run_id, self.receipt.receipt_id)

    def test_noncanonical_or_duplicate_json_cannot_become_a_receipt(self):
        self.store.publish(self.receipt)
        raw = self.records()[0][-2]
        changed = raw[:-1] + ',"schemaVersion":1}'
        self.corrupt(metadata_json=changed, metadata_sha256=sha256(changed.encode()).hexdigest())
        self.rejected(self.store.get, self.receipt.run_id, self.receipt.receipt_id)

    def test_historical_lookup_revalidates_immutable_source_bytes(self):
        self.store.publish(self.receipt)
        with self.repository._transaction() as connection:
            original = connection.execute("SELECT content FROM artifact_contents WHERE artifact_id=?",
                (str(self.source.artifact_id),)).fetchone()[0]
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            connection.execute("UPDATE artifact_contents SET content=? WHERE artifact_id=?", (
                sqlite3.Binary(b"x" * len(original)), str(self.source.artifact_id)))
        self.rejected(self.store.get, self.receipt.run_id, self.receipt.receipt_id)

    def test_source_configuration_and_workspace_ownership_are_revalidated_on_get(self):
        self.store.publish(self.receipt)
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER run_configurations_no_update")
            config = self.repository.get_run_configuration(self.receipt.run_id)
            changed = config.model_copy(update={"workspace_id": uuid4()})
            connection.execute("UPDATE run_configurations SET payload_json=? WHERE run_id=?",
                (changed.model_dump_json(), str(self.receipt.run_id)))
        self.rejected(self.store.get, self.receipt.run_id, self.receipt.receipt_id)

    def test_concurrent_identical_publication_remains_one_immutable_record(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = tuple(workers.map(lambda _index: self.store.publish(self.receipt), range(8)))
        self.assertTrue(all(receipt == self.receipt for receipt in results))
        self.assertEqual(len(self.records()), 1)

    def test_current_revalidation_source_gets_distinct_version_bound_receipt(self):
        self.store.publish(self.receipt)
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.fixture.mutate_run(status=WorkflowStatus.FIXING, code_version=1, fix_attempt=1)
        developer = WorkflowStep(run_id=self.receipt.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.RUNNING, code_version=2, requirement_ids=list(self.source.requirement_ids))
        self.fixture.insert_step(developer)
        artifacts = ArtifactStore(self.repository, self.fixture.registry)
        source = artifacts.bind(self.receipt.run_id, role=AgentRole.DEVELOPER).freeze_source(
            workflow_step_id=developer.workflow_step_id, commit_hash=self.source.commit_hash,
            repository_id=self.source.repository_id, lock_path="requirements.lock")
        self.register_source(source)
        self.fixture.mutate_step(developer, status=WorkflowStepStatus.SUCCEEDED)
        security = WorkflowStep(run_id=self.receipt.run_id, agent_role=AgentRole.SECURITY,
            status=WorkflowStepStatus.RUNNING, code_version=2, requirement_ids=list(self.ids),
            input_artifact_ids=[source.artifact_id])
        self.fixture.insert_step(security)
        self.fixture.mutate_run(status=WorkflowStatus.REVALIDATING, code_version=2, fix_attempt=1)
        new = replace(self.receipt, receipt_id=uuid4(), request_id=uuid4(), workflow_step_id=security.workflow_step_id,
            source_artifact_id=source.artifact_id, code_version=2, snapshot_sha256=source.snapshot_sha256,
            evidence_refs=(f"artifact://{source.artifact_id}/source.tar#path=main.py",))
        self.assertEqual(self.store.publish(new), new)
        self.assertEqual(self.store.get(new.run_id, new.receipt_id), new)
        self.assertEqual(self.store.get(self.receipt.run_id, self.receipt.receipt_id), self.receipt)
        self.rejected(self.store.publish, self.receipt)
