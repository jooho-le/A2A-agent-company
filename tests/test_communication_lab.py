"""Real A2A diagnostics: independent boundaries, known faults and no fake PASS."""

import asyncio
import json
import socket
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

import httpx
import uvicorn
from a2a.types import TaskState
from pydantic import ValidationError

from diagnostics.communication.contracts import (
    CheckOptions, ProbeRecord, compare, differences, fingerprint, grounding, summarize,
)
from diagnostics.communication.lab import CommunicationLab, EmbeddedServer, create_dashboard, running_lab
from orchestrator.a2a import A2AAgentClient


def sample_payload():
    return {"sample": {"value": 100, "label": "한글 원문", "requirementIds": ["R1", "R2"]}, "upstream": None}


def sample_response():
    return {"claims": {"value": 100, "label": "한글 원문", "requirementCount": 2, "upstreamHash": None}}


class DiagnosticOracleTests(unittest.TestCase):
    def test_struct_numbers_do_not_false_positive_but_strings_and_booleans_do(self):
        self.assertEqual(fingerprint({"x": 100}), fingerprint({"x": 100.0}))
        self.assertEqual(compare({"x": 100}, {"x": "100"})["status"], "FAIL")
        self.assertEqual(compare({"x": 1}, {"x": True})["status"], "FAIL")

    def test_nested_differences_keep_missing_added_order_and_unicode(self):
        diff = differences({"a/b": {"한글": [1, 2]}, "missing": None},
                           {"a/b": {"한글": [2, 1]}, "extra": True})
        self.assertTrue(any(item["path"] == "$/a~1b/한글/0" for item in diff))
        self.assertTrue(any(item["kind"] == "MISSING" for item in diff))
        self.assertTrue(any(item["kind"] == "ADDED" for item in diff))

    def test_unobserved_input_or_output_is_unverified(self):
        self.assertEqual(compare(None, {})["status"], "UNVERIFIED")
        self.assertEqual(grounding(None, sample_payload())["status"], "UNVERIFIED")
        self.assertEqual(grounding(sample_response(), None)["status"], "UNVERIFIED")
        summary = summarize([ProbeRecord("DEVELOPER", 1, 0, "normal")])
        self.assertEqual(summary["verifiedRequests"], 0)
        self.assertEqual(summary["unverifiedRequests"], 1)

    def test_wrong_number_and_unsupported_claim_are_independent_of_transport(self):
        response = sample_response()
        response["claims"]["value"] = 101
        response["claims"]["deploymentSucceeded"] = True
        self.assertEqual(compare(response, response)["status"], "PASS")
        verdict = grounding(response, sample_payload())
        self.assertEqual(verdict["status"], "FAIL")
        self.assertTrue(any(item["kind"] == "UNSUPPORTED_CLAIM" for item in verdict["differences"]))

    def test_changed_input_is_not_falsely_called_agent_hallucination(self):
        original, received, response = sample_payload(), sample_payload(), sample_response()
        received["sample"]["value"] = response["claims"]["value"] = 101
        self.assertEqual(compare(original, received)["status"], "FAIL")
        self.assertEqual(grounding(response, received)["status"], "PASS")
        self.assertEqual(grounding(response, original)["status"], "FAIL")

    def test_free_prose_is_explicitly_outside_grounding_scope(self):
        response = sample_response()
        response["freeText"] = "자유 서술의 사실성은 이 오라클에서 판정하지 않습니다."
        verdict = grounding(response, sample_payload())
        self.assertEqual(verdict["status"], "PASS")
        self.assertEqual(verdict["freeTextStatus"], "UNVERIFIED")
        self.assertEqual(verdict["scope"], "DECLARED_FIELDS_ONLY")

    def test_missing_claims_or_forged_upstream_hash_fail(self):
        self.assertEqual(grounding({}, sample_payload())["status"], "FAIL")
        payload = sample_payload()
        payload["upstream"] = {"answer": 100}
        self.assertEqual(grounding(sample_response(), payload)["status"], "FAIL")

    def test_input_limits_and_secret_label_redaction(self):
        for kwargs in ({"rounds": 6}, {"rounds": True}, {"processing_ms": -1},
                       {"value": 2 ** 53}, {"sender_delay_ms": 10001}, {"url": "http://elsewhere"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                CheckOptions(**kwargs)
        self.assertNotIn("DUMMY_SECRET", CheckOptions(label="password=DUMMY_SECRET").label)


class DiagnosticDashboardGuards(unittest.IsolatedAsyncioTestCase):
    async def test_origin_custom_header_host_and_payload_guards(self):
        lab = CommunicationLab()
        app = create_dashboard(lab, 8766)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as client:
            response = await client.get("/api/status")
            self.assertFalse(response.json()["llmUsed"])
            self.assertEqual((await client.post("/api/check", json={})).status_code, 403)
            headers = {"Origin": "http://attacker.invalid", "X-Lab-Action": "check"}
            self.assertEqual((await client.post("/api/check", json={}, headers=headers)).status_code, 403)
            headers["Origin"] = "http://127.0.0.1:8766"
            response = await client.post("/api/check", json={"rounds": True, "label": "password=DUMMY_SECRET"}, headers=headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("DUMMY_SECRET", response.text)
            self.assertEqual((await client.post("/api/check", content="x" * 4097, headers={**headers, "Content-Type": "application/json"})).status_code, 413)
            self.assertEqual((await client.get("/", headers={"Host": "attacker.invalid"})).status_code, 400)
            lab.busy = True
            self.assertEqual((await client.post("/api/check", json={}, headers=headers)).status_code, 409)


class DiagnosticRealHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_sigint_exits_after_agent_db_cleanup(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        root = Path(__file__).resolve().parents[1]
        process = subprocess.Popen([sys.executable, "-m", "diagnostics.communication", "--port", str(port)],
                                   cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=.3) as client:
                ready = False
                for _ in range(100):
                    try:
                        ready = (await client.get(f"http://127.0.0.1:{port}/api/status")).status_code == 200
                    except httpx.HTTPError:
                        pass
                    if ready:
                        break
                    await asyncio.sleep(.04)
                self.assertTrue(ready)
            process.send_signal(signal.SIGINT)
            output, errors = await asyncio.to_thread(process.communicate, timeout=8)
            self.assertEqual(process.returncode, 0, errors.decode(errors="replace"))
            self.assertNotIn(b"Traceback", errors)
            self.assertNotIn(b"unable to open database", errors)
        finally:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.communicate, timeout=8)

    async def test_browser_disconnect_drains_remote_task_and_releases_busy(self):
        async with running_lab() as lab:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(16)
            port = sock.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            server = EmbeddedServer(uvicorn.Config(create_dashboard(lab, port), log_level="error", lifespan="on"))
            serve = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(.02)
                async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                    async with client.stream("POST", "/api/check", json={"scenario": "no_response", "rounds": 1, "processing_ms": 0},
                                             headers={"Origin": origin, "X-Lab-Action": "check"}) as response:
                        async for line in response.aiter_lines():
                            event = json.loads(line)
                            if event["type"] == "row" and event["row"]["role"] == "DEVELOPER" and event["row"]["phase"] == "PROCESSING":
                                break
                    for _ in range(150):
                        if not lab.busy:
                            break
                        await asyncio.sleep(.02)
                    self.assertFalse(lab.busy)
                    row = next(row for row in lab.records.values() if row.role == "DEVELOPER")
                    async with httpx.AsyncClient(trust_env=False, headers={"Authorization": "Bearer " + lab.tokens[row.role]}) as transport:
                        agent = A2AAgentClient(lab.endpoints[row.role], httpx_client=transport)
                        task = await agent.get_task(row.task_id)
                        self.assertEqual(task.status.state, TaskState.TASK_STATE_CANCELED)
                    restarted = await client.post("/api/check", json={"architecture": "SINGLE_AGENT", "rounds": 1, "processing_ms": 0},
                                                  headers={"Origin": origin, "X-Lab-Action": "check"})
                    self.assertEqual(json.loads(restarted.text.splitlines()[-1])["summary"]["verifiedRequests"], 1)
            finally:
                server.should_exit = True
                await asyncio.wait_for(serve, timeout=8)
                sock.close()

    async def test_real_multi_single_faults_missing_response_and_stream(self):
        async with running_lab() as lab:
            async with httpx.AsyncClient(trust_env=False) as client:
                unauthorized = await client.get(lab.endpoints["DEVELOPER"] + "/.well-known/agent-card.json")
                self.assertEqual(unauthorized.status_code, 401)
            observed_events = []
            normal = await lab.execute(CheckOptions(rounds=2, processing_ms=10), observed_events.append)
            self.assertFalse(normal["llmUsed"])
            self.assertEqual(normal["summary"]["verifiedRequests"], 8)
            self.assertEqual(normal["summary"]["issueCount"], 0)
            self.assertTrue(any(event["type"] == "row" and event["row"]["phase"] == "PROCESSING" for event in observed_events))
            for row in normal["rows"]:
                self.assertEqual(row["observations"]["wireSent"]["sample"]["value"], 100)
                self.assertEqual(row["observations"]["executorReceived"]["sample"]["value"], 100)
                self.assertTrue(all(value is not None and value >= 0 for value in row["timings"].values()))
                self.assertGreaterEqual(row["timings"]["agentProcessingMs"], 9)
            developer = next(row for row in normal["rows"] if row["role"] == "DEVELOPER")
            self.assertIsNotNone(developer["observations"]["executorReceived"]["upstream"])

            # A Single Run owns one Context, including repeated lab probes.
            # A one-round test misses the admission failure from round two.
            single = await lab.execute(CheckOptions(architecture="SINGLE_AGENT", rounds=5, processing_ms=0), lambda event: None)
            self.assertEqual(single["summary"]["verifiedRequests"], 5)
            self.assertEqual(single["summary"]["issueCount"], 0)
            self.assertEqual(len({row["contextId"] for row in single["rows"]}), 1)
            self.assertEqual(len({row["taskId"] for row in single["rows"]}), 5)

            for scenario in ("request_change", "response_change", "grounding_error"):
                report = await lab.execute(CheckOptions(scenario=scenario, architecture="SINGLE_AGENT", rounds=1, processing_ms=0), lambda event: None)
                self.assertEqual(report["summary"]["issueCount"], 1)
                row = report["rows"][0]
                checks = row["checks"]
                if scenario == "request_change":
                    self.assertEqual(checks["request"]["network"]["status"], "PASS")
                    self.assertEqual(checks["request"]["admission"]["status"], "FAIL")
                    self.assertEqual(checks["grounding"]["status"], "PASS")
                elif scenario == "response_change":
                    self.assertEqual(checks["grounding"]["status"], "PASS")
                    self.assertEqual(checks["response"]["status"], "FAIL")
                else:
                    self.assertEqual(checks["request"]["endToEnd"]["status"], "PASS")
                    self.assertEqual(checks["response"]["status"], "PASS")
                    self.assertEqual(checks["grounding"]["status"], "FAIL")

            dropped = await lab.execute(CheckOptions(scenario="no_response", rounds=1, processing_ms=0), lambda event: None)
            self.assertEqual(dropped["summary"]["sent"], 2)
            self.assertEqual(dropped["summary"]["received"], 2)
            self.assertEqual(dropped["summary"]["responses"], 1)
            self.assertEqual([r["phase"] for r in dropped["rows"]], ["COMPLETED", "TIMEOUT", "SKIPPED", "SKIPPED"])
            self.assertIsNone(dropped["rows"][1]["timings"]["agentProcessingMs"])
            self.assertNotIn("취소 확인 실패", dropped["rows"][1]["error"])

            prepared = await lab.execute(CheckOptions(architecture="SINGLE_AGENT", rounds=1, processing_ms=0, sender_delay_ms=30), lambda event: None)
            self.assertGreaterEqual(prepared["rows"][0]["timings"]["orchestratorPrepareMs"], 28)

            app = create_dashboard(lab, 8766)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as client:
                response = await client.post("/api/check", json={"architecture": "SINGLE_AGENT", "rounds": 1, "processing_ms": 0},
                                             headers={"Origin": "http://127.0.0.1:8766", "X-Lab-Action": "check"})
                self.assertEqual(response.status_code, 200)
                events = [json.loads(line) for line in response.text.splitlines()]
                self.assertEqual(events[0]["type"], "start")
                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual(events[-1]["summary"]["verifiedRequests"], 1)
                self.assertFalse(lab.busy)
                for token in lab.tokens.values():
                    self.assertNotIn(token, response.text)


if __name__ == "__main__":
    unittest.main()
