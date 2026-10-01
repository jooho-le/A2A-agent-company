import logging


def configure_logging(level: str) -> None:
    """Configure the standard-library logger without replacing host handlers."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
