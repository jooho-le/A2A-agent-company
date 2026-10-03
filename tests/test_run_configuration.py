import json
import unittest
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.domain import RunConfiguration, RunConfigurationArtifact, SCN_001_ID


class RunConfigurationTests(unittest.TestCase):
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
