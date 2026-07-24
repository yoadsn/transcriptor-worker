"""Line extraction using Surya layout detection.

For each extracted page image this module:
1. Loads the image from the local temp directory (falls back to reading from
   target storage if the temp file is absent).
2. Runs the Surya :class:`~surya.detection.DetectionPredictor` to detect text
   line bounding boxes / polygons.
3. Serialises the per-line data to a JSON file and writes it to target storage.
4. Returns an updated :class:`~transcriptor_worker.models.PageRecord` with
   ``status="completed"``, ``image_filename``, and ``lines_filename`` set.

JSON output format (one file per page)::

    {
        "submission_id": "<id>",
        "image_filename": "<stem>.jpg",
        "image_width": 2550,
        "image_height": 3300,
        "raw_image_filename": "<stem>.avif",
        "raw_image_width": 3600,
        "raw_image_height": 4800,
        "lines": [
            {
                "index": 0,
                "bbox": [x_min, y_min, x_max, y_max],
                "polygon": [[x, y], ...],
                "confidence": 0.97
            },
            ...
        ]
    }

Threshold configuration
-----------------------
``DETECTOR_TEXT_THRESHOLD`` and ``DETECTOR_BLANK_THRESHOLD`` (from
:class:`~transcriptor_worker.config.Config`) override the Surya library
defaults when set.  Leave them unset (``None``) to use the library defaults.

Model initialisation
--------------------
:func:`init_surya_model` should be called **once per worker process** (e.g. in
the ``initializer`` argument of :class:`multiprocessing.Pool`).  The returned
predictor object is then passed to :func:`process_page_lines` for every page.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from PIL import Image

from transcriptor_worker.models import PageRecord
from transcriptor_worker.storage.base import StorageBackend

logger = logging.getLogger(__name__)


def init_surya_model(
    text_threshold: float | None = None,
    blank_threshold: float | None = None,
) -> Any:
    """Load the Surya detection model and return the predictor.

    This function is intentionally heavy — it loads model weights.  Call it
    **once** at sub-process startup and reuse the returned predictor object.

    Args:
        text_threshold: Override ``settings.DETECTOR_TEXT_THRESHOLD``.
                        ``None`` leaves the library default unchanged.
        blank_threshold: Override ``settings.DETECTOR_BLANK_THRESHOLD``.
                         ``None`` leaves the library default unchanged.

    Returns:
        A :class:`surya.detection.DetectionPredictor` instance ready for use.
    """
    from surya.settings import settings as surya_settings
    from surya.detection import DetectionPredictor

    if text_threshold is not None:
        surya_settings.DETECTOR_TEXT_THRESHOLD = text_threshold
        logger.debug("Surya DETECTOR_TEXT_THRESHOLD set to %s", text_threshold)

    if blank_threshold is not None:
        surya_settings.DETECTOR_BLANK_THRESHOLD = blank_threshold
        logger.debug("Surya DETECTOR_BLANK_THRESHOLD set to %s", blank_threshold)

    logger.info("Loading Surya DetectionPredictor…")
    predictor = DetectionPredictor()
    logger.info("Surya DetectionPredictor loaded.")
    return predictor


def extract_lines(page_image_path: str, model: Any) -> dict:
    """Run Surya line detection on a single page image.

    Args:
        page_image_path: Absolute or relative path to the page image on the
                         local filesystem.
        model: A :class:`surya.detection.DetectionPredictor` instance returned
               by :func:`init_surya_model`.

    Returns:
        A dict with keys ``"image_width"``, ``"image_height"``, and ``"lines"``.
        ``"lines"`` is a list of line dicts, each with ``"index"``, ``"bbox"``,
        ``"polygon"``, and ``"confidence"``.

    Raises:
        FileNotFoundError: If *page_image_path* does not exist.
        Exception: Re-raises any error from Surya detection.
    """
    image = Image.open(page_image_path).convert("RGB")
    image_width, image_height = image.size

    # DetectionPredictor expects a list of PIL images.
    results = model([image])

    # results[0] is a TextDetectionResult; .bboxes is List[PolygonBox]
    detection = results[0]
    lines = []
    for idx, box in enumerate(detection.bboxes):
        lines.append(
            {
                "index": idx,
                "bbox": [float(v) for v in box.bbox],
                "polygon": [[float(v) for v in pt] for pt in box.polygon],
                "confidence": float(box.confidence) if box.confidence is not None else None,
            }
        )

    return {
        "image_width": image_width,
        "image_height": image_height,
        "lines": lines,
    }


def process_page_lines(
    page_record: PageRecord,
    model: Any,
    target_storage: StorageBackend,
    temp_dir: str,
) -> PageRecord:
    """Orchestrate line detection for a single page and persist results.

    Workflow:
    1. Resolve the page image path — temp dir first, fall back to reading the
       image bytes from target storage and writing a temporary local copy.
    2. Call :func:`extract_lines` with the resolved local path.
    3. Serialise the result to JSON and write it to target storage under
       ``{submission_id}/{lines_filename}``.
    4. Return a copy of *page_record* updated with
       ``status="completed"``, ``lines_filename`` set.

    On any failure the returned record has ``status="failed"`` and ``error``
    set; the original record is not mutated.

    Args:
        page_record: The record for the page to process (must have
                     ``image_filename`` set).
        model: DetectionPredictor from :func:`init_surya_model`.
        target_storage: Storage backend where page images and JSON live.
        temp_dir: Root of local temp directory used during this run.

    Returns:
        Updated :class:`PageRecord`.
    """
    if not page_record.image_filename:
        return PageRecord(
            submission_id=page_record.submission_id,
            doc_filename=page_record.doc_filename,
            page_number=page_record.page_number,
            status="failed",
            error="image_filename is empty; cannot run line extraction",
            image_filename=page_record.image_filename,
            lines_filename=page_record.lines_filename,
            raw_image_filename=page_record.raw_image_filename,
            raw_image_width=page_record.raw_image_width,
            raw_image_height=page_record.raw_image_height,
        )

    image_filename = page_record.image_filename
    submission_id = page_record.submission_id

    # Derive the lines JSON filename from the image filename.
    image_basename = Path(image_filename).name
    lines_filename = f"{image_basename}.json"
    lines_target_path = f"{submission_id}/{lines_filename}"

    # ------------------------------------------------------------------
    # Resolve a local path for the page image.
    # ------------------------------------------------------------------
    temp_image_path = Path(temp_dir) / submission_id / image_filename

    local_image_path: str
    _temp_copy_written = False

    if temp_image_path.exists():
        local_image_path = str(temp_image_path)
        logger.debug("Using temp image: %s", local_image_path)
    else:
        # Temp file absent — read from target storage and write a local copy.
        logger.debug(
            "Temp image %s not found; falling back to target storage", temp_image_path
        )
        target_image_path = f"{submission_id}/{image_filename}"
        try:
            image_bytes = target_storage.read_bytes(target_image_path)
        except Exception as exc:
            return PageRecord(
                submission_id=submission_id,
                doc_filename=page_record.doc_filename,
                page_number=page_record.page_number,
                status="failed",
                error=f"Cannot read image from target storage ({target_image_path}): {exc}",
                image_filename=image_filename,
                lines_filename="",
                raw_image_filename=page_record.raw_image_filename,
                raw_image_width=page_record.raw_image_width,
                raw_image_height=page_record.raw_image_height,
            )

        # Write to temp so PIL / Surya can open it from disk.
        temp_image_path.parent.mkdir(parents=True, exist_ok=True)
        temp_image_path.write_bytes(image_bytes)
        local_image_path = str(temp_image_path)
        _temp_copy_written = True
        logger.debug("Wrote fallback temp image: %s", local_image_path)

    # ------------------------------------------------------------------
    # Run detection.
    # ------------------------------------------------------------------
    try:
        result = extract_lines(local_image_path, model)
    except Exception as exc:
        logger.warning(
            "Line extraction failed for %s/%s: %s", submission_id, image_filename, exc
        )
        return PageRecord(
            submission_id=submission_id,
            doc_filename=page_record.doc_filename,
            page_number=page_record.page_number,
            status="failed",
            error=f"Line extraction error: {exc}",
            image_filename=image_filename,
            lines_filename="",
            raw_image_filename=page_record.raw_image_filename,
            raw_image_width=page_record.raw_image_width,
            raw_image_height=page_record.raw_image_height,
        )
    finally:
        # Clean up temp copy written solely for fallback purposes.
        if _temp_copy_written and temp_image_path.exists():
            try:
                os.remove(temp_image_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Serialise and write JSON to target storage.
    # ------------------------------------------------------------------
    payload = {
        "submission_id": submission_id,
        "image_filename": image_filename,
        "image_width": result["image_width"],
        "image_height": result["image_height"],
        "raw_image_filename": page_record.raw_image_filename,
        "raw_image_width": page_record.raw_image_width,
        "raw_image_height": page_record.raw_image_height,
        "lines": result["lines"],
    }
    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        target_storage.write_bytes(lines_target_path, json_bytes)
    except Exception as exc:
        logger.warning(
            "Failed to write lines JSON %s: %s", lines_target_path, exc
        )
        return PageRecord(
            submission_id=submission_id,
            doc_filename=page_record.doc_filename,
            page_number=page_record.page_number,
            status="failed",
            error=f"Failed to write lines JSON: {exc}",
            image_filename=image_filename,
            lines_filename="",
            raw_image_filename=page_record.raw_image_filename,
            raw_image_width=page_record.raw_image_width,
            raw_image_height=page_record.raw_image_height,
        )

    logger.info(
        "Wrote %d line(s) for %s/%s → %s",
        len(result["lines"]),
        submission_id,
        image_filename,
        lines_target_path,
    )

    return PageRecord(
        submission_id=submission_id,
        doc_filename=page_record.doc_filename,
        page_number=page_record.page_number,
        status="completed",
        error="",
        image_filename=image_filename,
        lines_filename=lines_filename,
        raw_image_filename=page_record.raw_image_filename,
        raw_image_width=page_record.raw_image_width,
        raw_image_height=page_record.raw_image_height,
    )
