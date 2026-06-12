"""Source submission discovery."""

from __future__ import annotations

import logging

from transcriptor_worker.models import Submission
from transcriptor_worker.storage.base import StorageBackend

logger = logging.getLogger(__name__)


def discover_submissions(
    source_storage: StorageBackend,
    root_prefix: str,
) -> list[Submission]:
    """Discover all submissions under *root_prefix* in *source_storage*.

    A submission is identified by the presence of a ``desc.json`` file.
    The submission ID is the name of the immediate parent folder that contains
    ``desc.json`` (i.e., the leaf folder in the source hierarchy).

    Args:
        source_storage: Storage backend to search.
        root_prefix: Root path / prefix to start the recursive search from.

    Returns:
        List of :class:`Submission` objects, one per discovered ``desc.json``.
    """
    desc_paths = source_storage.walk(root_prefix, "desc.json")
    submissions: list[Submission] = []

    for desc_path in desc_paths:
        # Normalise: strip trailing slash, split on both / and OS sep
        # desc_path is e.g. "uploads/user-id/submission-id/desc.json"
        parts = desc_path.replace("\\", "/").split("/")
        if len(parts) < 2:
            logger.warning(
                "Unexpected desc.json path (too shallow, skipping): %s", desc_path
            )
            continue

        # The leaf folder is the parent of desc.json
        submission_id = parts[-2]
        # The source_path is the folder containing desc.json (strip filename)
        source_path = "/".join(parts[:-1])

        submissions.append(Submission(id=submission_id, source_path=source_path))
        logger.debug("Discovered submission %s at %s", submission_id, source_path)

    logger.info(
        "Discovery found %d submissions under %r", len(submissions), root_prefix
    )
    return submissions
