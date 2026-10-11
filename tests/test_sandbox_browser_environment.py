"""Fixed browser-only environment with fake Docker, no real image or browser."""

import copy
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime
from test_sandbox_runtime import CONTAINER_ID, IMAGE_ID, FakeDocker


class BrowserEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.docker = FakeDocker()
        self.runtime = SandboxRuntime(None, None, None, docker=self.docker, materializer=object())
        self.prepared = SimpleNamespace(source_root=Path("/fixture/source"), inputs_root=Path("/fixture/inputs"))

    def profile(self, tool="run_browser_tests"):
        return ExecutionProfile(name="fixture-env", tool_name=tool, argv=("/usr/local/bin/python", "-B", "/inputs/runner.py"))

    async def prepare_container(self, profile):
        image, env = await self.runtime._image(profile, IMAGE_ID, time.monotonic() + 60)
        args = self.runtime._create("fixture", {}, self.prepared, profile, image)
        self.docker._create(args[1:])
        return env, args

    def validate(self, container, env, profile):
        self.runtime._validate_container(container, container_id=CONTAINER_ID, name="fixture", labels={},
            prepared=self.prepared, profile=profile, image_id=IMAGE_ID, env=env)

    async def test_browser_path_injected_even_when_common_image_has_no_env(self):
        profile = self.profile()
        env, args = await self.prepare_container(profile)
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/ms-playwright")
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH=/ms-playwright", args)
        self.validate(self.docker.container, env, profile)

    async def test_other_three_tools_receive_no_browser_path(self):
        for tool in ("run_build", "run_unit_tests", "run_security_scan"):
            with self.subTest(tool=tool):
                profile = self.profile(tool)
                env, args = await self.prepare_container(profile)
                self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", env)
                self.assertFalse(any("PLAYWRIGHT_BROWSERS_PATH" in arg for arg in args))
                self.validate(self.docker.container, env, profile)

    async def test_host_browser_path_or_proxy_or_key_is_not_inherited(self):
        with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": "/secret/host",
                "HTTPS_PROXY": "http://host.invalid:80", "OPENAI_API_KEY": "fixture-private"}):
            env, args = await self.prepare_container(self.profile())
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/ms-playwright")
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("fixture-private", " ".join(args))

    async def test_wrong_embedded_browser_path_still_denied(self):
        self.docker.image[0]["Config"]["Env"] = ["PLAYWRIGHT_BROWSERS_PATH=/host/cache"]
        with self.assertRaises(SandboxError) as raised:
            await self.prepare_container(self.profile())
        self.assertEqual(raised.exception.code, SandboxErrorCode.DENIED)

    async def test_common_image_with_browser_env_still_rejected_for_build(self):
        self.docker.image[0]["Config"]["Env"] = ["PLAYWRIGHT_BROWSERS_PATH=/ms-playwright"]
        with self.assertRaises(SandboxError) as raised:
            await self.prepare_container(self.profile("run_build"))
        self.assertEqual(raised.exception.code, SandboxErrorCode.DENIED)

    async def test_inspection_rejects_removed_or_mutated_fixed_browser_env(self):
        profile = self.profile()
        env, _ = await self.prepare_container(profile)
        for replacement in (None, "PLAYWRIGHT_BROWSERS_PATH=/other"):
            container = copy.deepcopy(self.docker.container)
            container["Config"]["Env"] = [value for value in container["Config"]["Env"]
                                           if not value.startswith("PLAYWRIGHT_BROWSERS_PATH=")]
            if replacement is not None:
                container["Config"]["Env"].append(replacement)
            with self.assertRaises(SandboxError) as raised:
                self.validate(container, env, profile)
            self.assertEqual(raised.exception.code, SandboxErrorCode.INTEGRITY)


if __name__ == "__main__":
    unittest.main()
