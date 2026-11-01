"""Start Bootstrap transport; use run_host --architecture SINGLE_AGENT for execution."""

import uvicorn

from agents.core.config import AgentSettings
from agents.core.identity import SingleAgentIdentity


def main() -> None:
    settings = AgentSettings(role=SingleAgentIdentity.SINGLE_AGENT)
    uvicorn.run("agents.single.app:create_app", factory=True,
                host=settings.host, port=settings.listen_port,
                log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
