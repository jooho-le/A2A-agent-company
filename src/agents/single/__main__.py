"""Start the local Single A2A transport; product runtime is not yet connected."""

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
