import json
import unittest
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.domain import RunConfiguration, RunConfigurationArtifact, SCN_001_ID, WorkflowRun
from orchestrator.infrastructure import SQLiteWorkflowRepository
from jsonschema import Draft202012Validator, FormatChecker
from pathlib import Path


class RunConfigurationTests(unittest.TestCase):
    def test_single_configuration_remains_single_when_restored_from_database(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "host.sqlite3"
            repository = SQLiteWorkflowRepository(path)
            run = WorkflowRun(scenario_id=SCN_001_ID, request_text="Single 설정 저장")
            config = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id,
                                             workspace_id=run.workspace_id, configuration={"architecture": "SINGLE_AGENT"})
            repository.create_run(run, (), (), run_configuration=config)
            restored = SQLiteWorkflowRepository(path).get_run_configuration(run.run_id)
            self.assertEqual(restored, config)
            self.assertEqual(restored.configuration.architecture, "SINGLE_AGENT")

    def test_both_architectures_round_trip_through_frozen_artifact_and_schema(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas/project/run_configuration.schema.json").read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for architecture in ("MULTI_AGENT", "SINGLE_AGENT"):
            with self.subTest(architecture=architecture):
                artifact = RunConfigurationArtifact(
                    run_id=uuid4(), scenario_id=SCN_001_ID, workspace_id=uuid4(),
                    configuration={"architecture": architecture},
                )
                validator.validate(artifact.to_artifact_json())
                restored = RunConfigurationArtifact.model_validate_json(artifact.model_dump_json())
                self.assertEqual(restored.configuration.architecture, architecture)
                with self.assertRaises(ValidationError):
                    restored.configuration.architecture = "OTHER"
        self.assertEqual(RunConfiguration().architecture, "MULTI_AGENT")
        with self.assertRaises(ValidationError):
            RunConfiguration(architecture="OTHER")

    def test_local_configuration_does_not_invent_model_or_environment(self):
        config = RunConfiguration()
        self.assertIsNone(config.model)
        self.assertIsNone(config.environment)
        self.assertEqual(config.limits.max_fix_attempts, 3)
        with self.assertRaises(ValidationError):
            config.limits.max_fix_attempts = 5
        with self.assertRaises(ValidationError):
            RunConfiguration(limits={"maxToolRetries": 3})

    def test_experiment_requires_complete_baseline(self):
        with self.assertRaises(ValidationError):
            RunConfiguration(experiment_id=uuid4())
        config = RunConfiguration(
            experiment_id=uuid4(), starting_commit_hash="a" * 40,
            starting_snapshot_sha256="b" * 64,
            model={"provider": "test-provider", "modelId": "test-model", "temperature": 0},
            environment={"containerImageDigest": "sha256:" + "c" * 64, "dependencyLockHash": "sha256:" + "d" * 64, "hardwareProfile": "fixed-local"},
            limits={"runtimeBudgetMs": 60000}, protected_test_suite_ref="artifact://protected/tests",
            scanner_profile_ref="artifact://protected/scanner", warmup_state="COLD", execution_order=0,
        )
        self.assertEqual(config.architecture, "MULTI_AGENT")
        for architecture in ("MULTI_AGENT", "SINGLE_AGENT"):
            for reference in (None, "artifact://protected/tests"):
                variant = config.model_dump(by_alias=True)
                variant.update(architecture=architecture, protectedTestSuiteRef=reference)
                admitted = RunConfiguration.model_validate(variant)
                self.assertEqual(admitted.protected_test_suite_ref, reference)
                for missing in ("startingCommitHash", "startingSnapshotSha256", "model", "environment", "scannerProfileRef", "warmupState", "executionOrder"):
                    with self.subTest(architecture=architecture, missing=missing), self.assertRaises(ValidationError):
                        RunConfiguration.model_validate({**variant, missing: None})

    def test_scenario_policy_is_frozen_and_public_copy_is_detached(self):
        artifact = RunConfigurationArtifact(run_id=uuid4(), scenario_id=SCN_001_ID, workspace_id=uuid4())
        checksum = artifact.scenario_contract_sha256
        detached = artifact.scenario_contract
        detached["emailPolicy"]["localPartCase"] = "modified"
        self.assertEqual(artifact.scenario_contract["emailPolicy"]["localPartCase"], "lowercase")
        self.assertEqual(artifact.scenario_contract_sha256, checksum)
        self.assertEqual(RunConfigurationArtifact.model_validate_json(artifact.model_dump_json()), artifact)
        with self.assertRaises(ValidationError):
            artifact.frozen_scenario_contract_json = "{}"
        with self.assertRaises(ValidationError):
            RunConfigurationArtifact(run_id=uuid4(), scenario_id=SCN_001_ID, workspace_id=uuid4(), frozen_scenario_contract_json=json.dumps({"scenarioId": str(uuid4())}))
