"""Start one local Agent using its configured role and reserved port."""

import uvicorn

from agents.core.config import AgentSettings


def main() -> None:
    settings = AgentSettings()
    uvicorn.run(
        "agents.main:create_app", factory=True,
        host=settings.host, port=settings.listen_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
