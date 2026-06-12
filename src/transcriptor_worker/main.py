"""CLI entry point for the transcriptor worker pipeline."""

import logging

from transcriptor_worker.coordinator import run


def main() -> None:
    """Entry point called by the ``transcriptor-worker`` script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    run()


if __name__ == "__main__":
    main()
