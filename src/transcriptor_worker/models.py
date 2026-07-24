"""Domain model dataclasses for the transcriptor worker pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# desc.json models
# ---------------------------------------------------------------------------


@dataclass
class DocFile:
    """One entry in the ``files`` list of a ``desc.json`` manifest.

    All fields are stored as they appear in the JSON.  Only the fields used by
    the pipeline are declared explicitly; unknown fields are captured in *extra*.
    """

    stored_filename: str
    file_extension: str
    mime_type: str
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DocFile":
        known = {"stored_filename", "file_extension", "mime_type"}
        return cls(
            stored_filename=d.get("stored_filename", ""),
            file_extension=d.get("file_extension", ""),
            mime_type=d.get("mime_type", ""),
            extra={k: v for k, v in d.items() if k not in known},
        )


@dataclass
class DescJson:
    """Parsed representation of a ``desc.json`` submission manifest.

    Only the fields required by the pipeline are declared; the full raw dict
    is retained in *raw* so that it can be round-tripped to the target
    unchanged.
    """

    version: str
    user: str
    files: list[DocFile]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DescJson":
        files = [DocFile.from_dict(f) for f in d.get("files", [])]
        return cls(
            version=str(d.get("version", "")),
            user=str(d.get("user", "")),
            files=files,
            raw=d,
        )


# ---------------------------------------------------------------------------
# Manifest row models
# ---------------------------------------------------------------------------


@dataclass
class SubmissionRecord:
    """One row in ``submissions.csv``."""

    submission_id: str
    status: str  # "completed" | "failed" | "pending"
    error: str = ""

    # CSV column order
    CSV_FIELDS: list[str] = field(
        default_factory=lambda: ["submission_id", "status", "error"],
        init=False,
        repr=False,
    )

    def to_dict(self) -> dict[str, str]:
        return {
            "submission_id": self.submission_id,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "SubmissionRecord":
        return cls(
            submission_id=d["submission_id"],
            status=d["status"],
            error=d.get("error", ""),
        )


# CSV_FIELDS is a class-level constant — make it accessible without instantiation
SubmissionRecord.CSV_FIELDS = ["submission_id", "status", "error"]  # type: ignore[assignment]


@dataclass
class PageRecord:
    """One row in ``pages.csv``."""

    submission_id: str
    doc_filename: str
    page_number: int
    status: str  # "completed" | "failed" | "pending"
    error: str = ""
    image_filename: str = ""
    lines_filename: str = ""
    raw_image_filename: str = ""
    raw_image_width: int = 0
    raw_image_height: int = 0

    # CSV column order
    CSV_FIELDS: list[str] = field(
        default_factory=lambda: [
            "submission_id",
            "doc_filename",
            "page_number",
            "status",
            "error",
            "image_filename",
            "lines_filename",
            "raw_image_filename",
            "raw_image_width",
            "raw_image_height",
        ],
        init=False,
        repr=False,
    )

    def to_dict(self) -> dict[str, str]:
        return {
            "submission_id": self.submission_id,
            "doc_filename": self.doc_filename,
            "page_number": str(self.page_number),
            "status": self.status,
            "error": self.error,
            "image_filename": self.image_filename,
            "lines_filename": self.lines_filename,
            "raw_image_filename": self.raw_image_filename,
            "raw_image_width": str(self.raw_image_width),
            "raw_image_height": str(self.raw_image_height),
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "PageRecord":
        return cls(
            submission_id=d["submission_id"],
            doc_filename=d["doc_filename"],
            page_number=int(d["page_number"]),
            status=d["status"],
            error=d.get("error", ""),
            image_filename=d.get("image_filename", ""),
            lines_filename=d.get("lines_filename", ""),
            raw_image_filename=d.get("raw_image_filename", ""),
            raw_image_width=int(d.get("raw_image_width") or 0),
            raw_image_height=int(d.get("raw_image_height") or 0),
        )


PageRecord.CSV_FIELDS = [  # type: ignore[assignment]
    "submission_id",
    "doc_filename",
    "page_number",
    "status",
    "error",
    "image_filename",
    "lines_filename",
    "raw_image_filename",
    "raw_image_width",
    "raw_image_height",
]


# ---------------------------------------------------------------------------
# Submission (in-flight work item)
# ---------------------------------------------------------------------------


@dataclass
class Submission:
    """A discovered submission to be processed.

    *source_path* is the path to the submission folder in source storage
    (includes the submission_id as the final component).
    """

    id: str
    source_path: str
