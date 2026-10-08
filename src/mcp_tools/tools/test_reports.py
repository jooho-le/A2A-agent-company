"""One existing QA report Tool; route only fixed private receipt namespaces."""

from types import MappingProxyType

from mcp_tools.runtime import MCPConfigurationError
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.unit import UnitTestTools
from orchestrator.domain.states import AgentRole


class TestReportTools:
    def __init__(self, unit, browser):
        if not isinstance(unit, UnitTestTools) or not isinstance(browser, BrowserTestTools):
            raise MCPConfigurationError()
        self._unit, self._browser = unit, browser

    def __repr__(self):
        return "TestReportTools()"

    def handlers(self, role):
        if not isinstance(role, AgentRole):
            raise MCPConfigurationError()
        return MappingProxyType({"read_test_report": self.read_test_report} if role is AgentRole.QA else {})

    async def read_test_report(self, context, arguments):
        # The dispatcher already validates artifact URI grammar. Each Store
        # still validates exact path, Run/Workspace ownership and Source grant.
        reader = self._browser if arguments["reportRef"].endswith("/browser-test-report.json") else self._unit
        return await reader.read_test_report(context, arguments)
