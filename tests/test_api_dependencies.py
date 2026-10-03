import unittest

from unittest.mock import patch

from orchestrator.api.dependencies import agent_client_factory
from orchestrator.core.config import Settings


class AgentAuthenticationTests(unittest.TestCase):
    def test_secrets_only_become_http_headers_for_the_selected_endpoint(self):
        settings = Settings(
            _env_file=None,
            planner_agent_url="https://planner.example.test/",
            planner_bearer_token="operator-credential",
        )
        with patch("orchestrator.api.dependencies.A2AAgentClient") as client:
            factory = agent_client_factory(settings)
            factory("https://planner.example.test/")
            client.assert_called_once_with("https://planner.example.test/", headers={"Authorization": "Bearer operator-credential"})
            factory("https://qa.example.test")
            self.assertIsNone(client.call_args.kwargs["headers"])
        self.assertNotIn("operator-credential", repr(settings))

    def test_shared_endpoint_cannot_mix_incompatible_credentials(self):
        settings = Settings(_env_file=None,
            planner_agent_url="https://agent.example.test/", planner_bearer_token="one",
            developer_agent_url="https://agent.example.test", developer_bearer_token="two",
        )
        with self.assertRaises(ValueError):
            agent_client_factory(settings)
