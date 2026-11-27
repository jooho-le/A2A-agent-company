"""공개 승인 GET만 검사합니다. HTTP는 Mock, DB는 기존 임시 SQLite fixture입니다."""

import json
import os
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

import jwt
from sqlalchemy import inspect

import test_host_reads
from approved_configuration import _host_contract


class ApprovedConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_host_reads.HostReadAPITests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.request = self.fixture.request
        self.connection = self.fixture.connection
        self.response = self.fixture.response
        self.scenario_id = self.fixture.fixture.scenario_id
        self.path = "/api/v1/host/approved-configuration?scenarioId=" + self.scenario_id
        # 운영 승인값이 아닌 격리 테스트용 객체입니다. Host 도메인 스키마로 직렬화합니다.
        schema, _ = _host_contract()
        self.configuration = schema.model_validate({
            "architecture": "MULTI_AGENT",
            "model": {"provider": "test-provider", "modelId": "offline-only", "temperature": 0.0},
            "limits": {"runtimeBudgetMs": 1000},
            "environment": {"containerImageDigest": "sha256:" + "a" * 64,
                            "dependencyLockHash": "sha256:" + "b" * 64, "hardwareProfile": "mock-cpu"},
            "startingCommitHash": "c" * 40, "startingSnapshotSha256": "d" * 64,
            "warmupState": "오프라인 검증",
        }).model_dump(mode="json", by_alias=True)
        self.before = self.snapshot()
        self.addCleanup(lambda: self.assertEqual(self.snapshot(), self.before))

    def snapshot(self):
        engine = self.fixture.fixture.engine
        with engine.connect() as db:
            return {table: db.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1').all()
                    for table in inspect(engine).get_table_names()}

    def envelope(self, configuration=None):
        value = deepcopy(self.configuration if configuration is None else configuration)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"scenarioId": self.scenario_id, "configurationId": sha256(raw.encode("utf-8")).hexdigest(),
                "configuration": value}

    def respond(self, result):
        self.response.read.return_value = json.dumps(result, ensure_ascii=False).encode("utf-8")

    def test_authenticated_public_configuration_preserves_host_hash_and_fields(self):
        result = self.envelope()
        self.respond(result)
        self.assertEqual(self.request("GET", self.path), (200, result))
        self.assertIsNone(result["configuration"]["model"]["seed"])
        self.assertIsNone(result["configuration"]["experimentId"])
        self.connection.request.assert_called_once_with("GET", self.path, headers={
            "Accept": "application/json", "Authorization": "Bearer mock-host-service-token-for-tests-only"})
        self.connection.close.assert_called()
        self.response.close.assert_called()

    def test_jwt_and_scenario_rejections_never_contact_host(self):
        expired = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
                             self.fixture.fixture.secret, algorithm="HS256")
        for token in ("", "invalid", expired):
            self.assertEqual(self.request("GET", self.path, user=None, token=token)[0], 401)
        for query in ("", "?scenarioId=invalid", "?scenarioId=" + str(uuid4()),
                      "?scenarioId=" + self.scenario_id + "&scenarioId=" + self.scenario_id,
                      "?scenarioId=" + self.scenario_id + "&url=http://example.test"):
            self.assertEqual(self.request("GET", "/api/v1/host/approved-configuration" + query)[0], 422)
        self.fixture.constructor.assert_not_called()

    def test_unready_host_errors_and_unsafe_configuration_are_blocked(self):
        unavailable = (503, {"detail": "승인된 공개 실행 설정을 조회할 수 없습니다."})
        for setting in ({"A2A_HOST_READS_ENABLED": "false"}, {"A2A_HOST_API_TOKEN": ""}):
            with patch.dict(os.environ, setting):
                self.assertEqual(self.request("GET", self.path), unavailable)
        self.fixture.constructor.assert_not_called()
        self.connection.request.side_effect = TimeoutError("private upstream detail")
        self.assertEqual(self.request("GET", self.path), unavailable)
        self.connection.request.side_effect = None
        for status in (401, 403, 404, 503):
            self.response.status = status
            self.response.read.reset_mock()
            expected = (502, {"detail": "Host 서비스 인증에 실패했습니다."}) if status == 401 else unavailable
            self.assertEqual(self.request("GET", self.path), expected)
            self.response.read.assert_not_called()
        self.response.status = 200

        bad = []
        for field, value in (("apiKey", "mock-private-value"), ("source", {"path": "/srv/private/source"})):
            configuration = deepcopy(self.configuration)
            configuration[field] = value
            bad.append(self.envelope(configuration))
        for value in ("sk-test-only-private-value", "Bearer mock-role-token", "/srv/private/runtime.json",
                      "C:\\private\\config.json", "file:///private/source", "%2Fsrv%2Fprivate",
                      "https://user:password@example.test/model", "api_key=mock-private-value",
                      "mock-host-service-token-for-tests-only"):
            configuration = deepcopy(self.configuration)
            configuration["model"]["modelRevision"] = value
            bad.append(self.envelope(configuration))
        nested = deepcopy(self.configuration)
        nested["model"]["a2aToken"] = "private-value"
        bad.append(self.envelope(nested))
        missing = deepcopy(self.configuration)
        del missing["experimentId"]
        bad.append(self.envelope(missing))
        bad.extend([
            {**self.envelope(), "configurationId": "0" * 64},
            {**self.envelope(), "scenarioId": str(uuid4())},
            {**self.envelope(), "serverPath": "/private/runtime.json"},
        ])
        for result in bad:
            self.respond(result)
            self.assertEqual(self.request("GET", self.path), unavailable)
        self.response.read.return_value = b'{"configuration": NaN}'
        self.assertEqual(self.request("GET", self.path), unavailable)
        self.assertTrue(all(call.args[0] == "GET" for call in self.connection.request.call_args_list))


if __name__ == "__main__":
    unittest.main()
