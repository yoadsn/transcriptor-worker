# Pipeline Implementation Plan

This plan implements the worker described in `pipeline-design.md`. It is organized into phases that build on each other. Each phase produces runnable, testable code.

Important - use this document for tracking your progress - whenever a phase is completed. Or if you stop your walk mid-phase Add a section to the end of the face. "Current Status". That explains what was done and what is left to do. 

**Key deviations from the design doc:**

- **pyMuPDF** (`pymupdf`) replaces ImageMagick for PDF page rasterization.
- An **image transformation step** is introduced between page extraction and line extraction. It is a no-op today (pass-through) but provides the hook for future deskewing, binarization, contrast enhancement, etc.

---

## Phase 1 — Project scaffolding and storage abstraction

### Goal

Establish the package structure, dependencies, configuration loading, and a storage abstraction that lets the rest of the code work identically against local filesystem and S3.

### Steps

1. **Create package layout:**
   ```
   src/
     transcriptor_worker/
       __init__.py
       config.py
       storage/
         __init__.py
         base.py
         local.py
         s3.py
       models.py
   ```

2. **Add dependencies to `pyproject.toml`:**
   ```
   dependencies = [
       "boto3",
       "pymupdf",
       "surya-ocr",
       "pillow",
   ]
   ```
   Run `uv sync` to install.

3. **`config.py`** — Load all environment variables from `pipeline-design.md § Configuration` into a typed dataclass. Validate presence and format on construction. Provide a `from_env()` classmethod. Include `TEMP_DIR` defaulting to `tempfile.gettempdir()`.

4. **`storage/base.py`** — Define a `StorageBackend` protocol (or ABC):
   - `list_prefixes(prefix: str) -> list[str]` — list immediate child "directories"
   - `list_files(prefix: str) -> list[str]` — list files under a prefix (non-recursive)
   - `walk(prefix: str, filename: str) -> list[str]` — recursively find all paths ending in `filename`
   - `read_bytes(path: str) -> bytes`
   - `read_text(path: str) -> str`
   - `write_bytes(path: str, data: bytes) -> None`
   - `write_text(path: str, data: str) -> None`
   - `exists(path: str) -> bool`

5. **`storage/local.py`** — Implement `StorageBackend` for local filesystem using `pathlib`.

6. **`storage/s3.py`** — Implement `StorageBackend` for S3 using boto3. Wrap I/O operations with retry logic (3 attempts, exponential backoff). Construct the client from source/target-specific AWS credentials.

7. **`models.py`** — Define dataclasses:
   - `DescJson` — mirrors the `desc.json` schema (version, user, form_metadata, files list, browser). Include a `from_dict(d)` classmethod.
   - `DocFile` — represents one entry in `desc.json.files` (stored_filename, file_extension, mime_type, etc.).
   - `SubmissionRecord` — a row in `submissions.csv` (submission_id, status, error).
   - `PageRecord` — a row in `pages.csv` (submission_id, doc_filename, page_number, status, error, image_filename, lines_filename).

### Verification

- Unit test `config.py` with mock env vars.
- Unit test local storage against a temp directory.
- Confirm `uv sync` installs all deps cleanly.

---

## Phase 2 — Source discovery and manifest management

### Goal

Implement stages 1 and 2 from the design: discover submissions in source storage, load existing target manifests, and determine which submissions need processing.

### Steps

1. **`src/transcriptor_worker/manifest.py`:**
   - `load_submissions_csv(storage, path) -> dict[str, SubmissionRecord]` — parse CSV into a dict keyed by submission_id. Return empty dict if file doesn't exist.
   - `load_pages_csv(storage, path) -> list[PageRecord]` — parse CSV into a list. Return empty list if file doesn't exist.
   - `save_submissions_csv(records, path)` — write list of `SubmissionRecord` to CSV.
   - `save_pages_csv(records, path)` — write list of `PageRecord` to CSV.
   - Use `csv.DictReader`/`DictWriter` from stdlib.

2. **`src/transcriptor_worker/discovery.py`:**
   - `discover_submissions(source_storage, root_prefix) -> list[Submission]` — use `storage.walk(root_prefix, "desc.json")` to find all submissions. For each hit, derive `submission_id` from the parent folder name. Return a list of `Submission(id, source_path)` objects.

3. **`src/transcriptor_worker/coordinator.py`** (skeleton):
   - Load target `submissions.csv`.
   - Run discovery.
   - Filter: exclude submissions already `completed` in the manifest.
   - Build a work queue of submissions to process.

### Verification

- Run discovery against `data/uploads/` with local storage and confirm it finds all 87 `desc.json` files.
- Round-trip test: write a manifest, read it back, compare.

---

## Phase 3 — Page extraction (PDF + image handling)

### Goal

Implement stage 3: for each submission, read its docs, extract page images from PDFs using pyMuPDF, pass through image files as-is, and write results to target storage plus local temp.

### Steps

1. **`src/transcriptor_worker/extraction/__init__.py`**

2. **`src/transcriptor_worker/extraction/pages.py`:**
   - `extract_pages(submission, source_storage, target_storage, temp_dir) -> list[PageRecord]`
   - Read `desc.json` from source, parse with `DescJson.from_dict()`.
   - For each file in `desc.json.files`:
     - Determine if it's a PDF (by extension or MIME type).
     - **If PDF:** open with `pymupdf.open()` from bytes (use `stream` parameter). For each page, render to pixmap at a target DPI (default 300), convert to JPEG bytes. Derive filename: `{doc_basename}_p{N}.jpg` (1-based). Write to target storage under `{submission_id}/{filename}`. Also write to `{temp_dir}/{submission_id}/{filename}` for downstream use.
     - **If image:** read raw bytes from source. Derive filename: `{doc_basename}_p1.{ext}`. Write to target and temp using same convention.
   - Return a `PageRecord` per page (status=`pending` at this point — line extraction hasn't run yet).
   - On per-doc failure: create a `PageRecord` with status=`failed` and error message; continue with remaining docs.

3. **`src/transcriptor_worker/extraction/image_transform.py`:**
   - `transform_image(image_bytes: bytes, image_format: str) -> bytes`
   - Today: return `image_bytes` unchanged (no-op).
   - Docstring explains this is the hook for future deskewing, binarization, contrast, resampling.
   - The page extraction step calls this function on every page image (whether from PDF or raw image) before writing to target/temp storage.

Note! For local target - temp can be == local target. Only for remote target we need a local temp.

### Verification

- Process a submission with a multi-page PDF and confirm correct number of page images written.
- Process a submission with a standalone JPEG and confirm it's copied through.
- Confirm image_transform is called but output is identical to input.
- Inspect generated filenames for correctness.

---

## Phase 4 — Line extraction (Surya)

### Goal

Implement stage 4: run Surya layout detection on each extracted page image, write line polygon JSON to target storage, update page records.

### Steps

1. **`src/transcriptor_worker/extraction/lines.py`:**
   - `init_surya_model() -> model` — load the Surya detection model once. This will be called at sub-process startup.
   - `extract_lines(page_image_path: str, model) -> dict` — load image from local path, run Surya detection, return raw result dict.
   - `process_page_lines(page_record, model, target_storage, temp_dir) -> PageRecord` — orchestrates: load image from temp (fallback to target storage), call `extract_lines`, write JSON to target, update the `PageRecord` with status=`completed`, `image_filename`, `lines_filename`. On failure: set status=`failed` with error.

2. **JSON output:** The lines JSON filename is derived from the image filename: `{image_basename}.json`. Written to `{submission_id}/{lines_filename}` on target.

Specifically, make sure the following information is extracted from a page:
- all lines
- for each line:
   - index (0 based) of line as the layout detected (in order)
   - bbox
   - polygon
   - detection confidence

A good place to start is an example (not a perfect match to our needs):

```
from surya.settings import settings
from surya.detection import DetectionPredictor
from surya.input.load import load_from_folder, load_from_file

settings.DETECTOR_TEXT_THRESHOLD = 0.5
settings.DETECTOR_BLANK_THRESHOLD = 0.2

IMAGE_PATH = './2.jpg'
images, names = load_from_file(IMAGE_PATH)
det_predictor = DetectionPredictor()

# predictions is a list of dicts, one per image
predictions = det_predictor(images)

#predictions[] - one result per image sent
#predictions[0].bboxes =
#[PolygonBox(polygon=[[717, 40], [993, 46], [992, 109], [716, 103]], confidence=0.6680476665496826, bbox=[716, 40, 993, 109]),
# ...
# PolygonBox(polygon=[[38, 1902], [877, 1902], [877, 2199], [38, 2199]], confidence=0.9606421589851379, bbox=[38, 1902, 877, 2199])]
```

The "thresholds" set - should be configurable, defaults are empty and leave the library to use internal defaults.

### Verification

- Run line extraction on a single page image and inspect the output JSON.
- Confirm the JSON file is written to the correct target path.
- Confirm fallback: delete temp file, verify it reads from target storage instead.

---

## Phase 5 — Coordinator and multiprocessing

### Goal

Wire everything together: the coordinator orchestrates discovery, filtering, dispatching to worker pools, desc.json copying, and manifest upload.

### Steps

1. **`src/transcriptor_worker/coordinator.py`** (full implementation):
   - `run()` — the main entry point:
     1. Load config via `Config.from_env()`.
     2. Run startup checks (validate credentials by performing a lightweight storage operation on both source and target).
     3. Discover submissions.
     4. Load existing manifests from target.
     5. Filter to pending/failed submissions.
     6. Initialize working CSV files in temp dir, pre-populated with carried-forward rows.
     7. Process submissions (see below).
     8. Upload final manifests to target.

2. **`src/transcriptor_worker/worker.py`:**
   - `process_submission(submission, source_storage_config, target_storage_config, temp_dir) -> tuple[SubmissionRecord, list[PageRecord]]`
     - This is the function dispatched to sub-processes.
     - Constructs storage backends from config (can't pickle boto3 clients, so reconstruct in sub-process).
     - Calls page extraction.
     - Calls image transform on each page (already integrated in phase 3).
     - Calls line extraction on each extracted page.
     - Copies `desc.json` to target.
     - Returns the submission record and all page records.
   - `init_worker(config)` — sub-process initializer that preloads the Surya model into a global.

3. **Multiprocessing orchestration in `coordinator.py`:**
   - Use `multiprocessing.Pool` with `initializer=init_worker`.
   - Map `process_submission` across the work queue.
   - Collect results and append to working CSVs.
   - Parallelism controlled by `WORKER_PARALLELISM` env var.

4. **Update `main.py`:**
   - Import and call `coordinator.run()`.
   - Add basic logging setup (stdlib `logging`, structured format with timestamps).

### Verification

- End-to-end run against `data/uploads/` (source=local) → `data/output/` (target=local).
- Confirm `submissions.csv` and `pages.csv` are written with correct content.
- Confirm page images and line JSONs are written under each submission folder.
- Confirm `desc.json` is copied to each submission folder on target.
- Re-run and confirm completed submissions are skipped.

---

## Phase 6 — Error handling, logging, and startup checks

### Goal

Harden the pipeline with proper error handling, structured logging, and startup validation.

### Steps

1. **Startup checks in `coordinator.py`:**
   - Validate all required env vars are present.
   - Test source storage read access (e.g., list a prefix).
   - Test target storage write access (e.g., write and delete a probe file).
   - Exit with descriptive error on failure.

2. **Logging:**
   - Configure `logging` at module level throughout the codebase.
   - Log at INFO: submission start/complete, page counts, timing.
   - Log at WARNING: page-level failures.
   - Log at ERROR: submission-level failures, storage errors.
   - Log at DEBUG: individual file operations, retry attempts.

3. **Retry wrapper:**
   - `src/transcriptor_worker/storage/retry.py` — a decorator or utility that retries a callable up to 3 times with exponential backoff (0.5s, 1s, 2s). Used by S3 storage for write operations.

4. **Graceful failure recording:**
   - Verify that any exception during submission processing is caught, recorded in the submission record as `failed` with the error message, and the run continues.
   - Verify page-level failures don't abort the submission.

### Verification

- Test with invalid AWS credentials — confirm clean error message and exit.
- Test with a corrupted PDF in a submission — confirm the submission is marked failed but others proceed.
- Inspect logs for completeness and readability.

---

## Phase 7 — CLI polish and dev tooling

### Goal

Make the worker easy to run, test, and develop against.

### Steps

1. **`.env.example`** — document all env vars with example values for local dev:
   ```
   SOURCE_STORAGE_TYPE=local
   SOURCE_STORAGE_PATH=./data/uploads
   TARGET_STORAGE_TYPE=local
   TARGET_STORAGE_PATH=./data/output
   WORKER_PARALLELISM=4
   ```

2. **Add dev dependencies** to `pyproject.toml`:
   ```toml
   [project.optional-dependencies]
   dev = ["pytest", "pytest-cov"]
   ```

3. **Add a script entry point** to `pyproject.toml`:
   ```toml
   [project.scripts]
   transcriptor-worker = "transcriptor_worker.main:main"
   ```
   Move the entry point logic from `main.py` to `src/transcriptor_worker/main.py`.

4. **Tests** — `tests/` directory with:
   - `test_config.py` — env var parsing and validation.
   - `test_storage_local.py` — local storage backend operations.
   - `test_manifest.py` — CSV round-trip.
   - `test_discovery.py` — discovery against a fixture directory.
   - `test_pages.py` — PDF and image extraction with small fixture files.
   - `test_image_transform.py` — no-op transform returns input unchanged.

### Verification

- `uv run pytest` passes all tests.
- `uv run transcriptor-worker` runs the pipeline end-to-end against local fixtures.

---

## Module dependency graph

```
main.py
  └── coordinator
        ├── config
        ├── discovery
        ├── manifest
        ├── worker
        │     ├── extraction/pages
        │     │     └── extraction/image_transform
        │     ├── extraction/lines
        │     └── storage/*
        └── storage/*
              ├── storage/base (protocol)
              ├── storage/local
              └── storage/s3
                    └── storage/retry
```

---

## Notes

- **pyMuPDF** (`pymupdf` on PyPI) provides `fitz.open()` which can open PDFs from bytes via the `stream` parameter. Each page is rasterized with `page.get_pixmap(dpi=300)` and converted to JPEG/PNG bytes via `pixmap.tobytes("jpeg")`.
- **Image transform no-op:** The `transform_image()` function receives raw image bytes and returns them unchanged. It sits in the pipeline between page extraction and writing to storage, so future transforms (Pillow-based deskew, binarization, contrast) can be added without restructuring.
- **Surya model loading:** The model is loaded once per worker process via the Pool initializer, not once per page. This is critical for performance.
- **CSV concurrency:** Since `multiprocessing.Pool.map()` collects results in the coordinator, there's no concurrent write contention on CSV files. The coordinator writes all results after collecting them from the pool.
- **Temp file cleanup:** The design doc says the OS handles temp cleanup. Use `tempfile.mkdtemp()` for the run's working directory. Optionally clean up at the end of a successful run.

---

## Current Status

**Last updated:** 2026-06-12

### Phase 1 — COMPLETED

All steps done and verified:

- `src/transcriptor_worker/__init__.py` — package root
- `src/transcriptor_worker/config.py` — `Config` dataclass with `from_env()`, validates all env vars, raises `ConfigError` on bad input
- `src/transcriptor_worker/models.py` — `DescJson`, `DocFile`, `SubmissionRecord`, `PageRecord`, `Submission` dataclasses with `from_dict()` / `to_dict()` helpers
- `src/transcriptor_worker/storage/base.py` — `StorageBackend` ABC
- `src/transcriptor_worker/storage/local.py` — `LocalStorageBackend` (pathlib-backed)
- `src/transcriptor_worker/storage/s3.py` — `S3StorageBackend` (boto3-backed, per-client credentials)
- `src/transcriptor_worker/storage/retry.py` — `with_retry()` utility (3 attempts, 0.5/1/2s back-off)
- `src/transcriptor_worker/storage/__init__.py`
- `src/transcriptor_worker/extraction/__init__.py` — placeholder for Phase 3
- `src/transcriptor_worker/main.py` — CLI entry point calling `coordinator.run()`
- `pyproject.toml` — dependencies added (`boto3`, `pymupdf`, `surya-ocr`, `pillow`), dev extras, script entry point, hatchling build
- `uv sync` — all deps installed successfully

### Phase 2 — COMPLETED

All steps done and verified:

- `src/transcriptor_worker/manifest.py` — `load_submissions_csv`, `load_pages_csv`, `save_submissions_csv`, `save_pages_csv` using `csv.DictReader/DictWriter`; handles missing files gracefully
- `src/transcriptor_worker/discovery.py` — `discover_submissions()` using `storage.walk(..., "desc.json")`; derives submission ID from parent folder name
- `src/transcriptor_worker/coordinator.py` — skeleton with `_build_storage()` factory, `build_work_queue()` (discovery + manifest loading + filtering), and `run()` stub

**Verification results:**
- Discovery against `data/uploads/` finds exactly **87 submissions** ✓
- CSV round-trip test (write + read back for both `submissions.csv` and `pages.csv`) passes ✓
- All module imports succeed ✓

### Phase 3 — COMPLETED

All steps done and verified:

- `src/transcriptor_worker/extraction/image_transform.py` — `transform_image()` no-op pass-through; returns input bytes unchanged. Docstring explains future hook for deskewing, binarization, contrast, resampling.
- `src/transcriptor_worker/extraction/pages.py` — `extract_pages()` main function plus helpers:
  - `_is_pdf()` — detects PDF by extension (`.pdf`) with MIME-type fallback; handles `null` MIME type correctly
  - `_extract_pdf_pages()` — opens PDF from bytes via `pymupdf.open(stream=..., filetype="pdf")`, rasterizes at 300 DPI with `page.get_pixmap(dpi=300).tobytes("jpeg")`, one record per page
  - `_extract_image_page()` — re-encodes image to JPEG via `pymupdf.Pixmap`, handles CMYK/alpha conversion, always produces page number 1
  - `_write_page()` — calls `transform_image()`, writes to both target storage (`{submission_id}/{filename}`) and local temp dir

**Verification results:**
- 12-page PDF → 12 records, all `status=pending`, correct filenames (`0_p1.jpg`…`0_p12.jpg`), files written to both target and temp ✓
- Single JPEG → 1 record, `status=pending`, `image_filename=0_p1.jpg` ✓
- 3-image submission → 3 records (one per doc file, each page 1): `0_p1.jpg`, `1_p1.jpg`, `2_p1.jpg` ✓
- 2-PDF submission → 69 records (62 + 7 pages), correct per-doc filenames ✓
- `transform_image` no-op returns the same object unchanged ✓

**Design note:** `mime_type` is `null` in many real `desc.json` files; PDF detection falls back to file extension (`.pdf`) which is always present.

### Phase 4 — COMPLETED

All steps done and verified:

- `src/transcriptor_worker/extraction/lines.py` — three public functions:
  - `init_surya_model(text_threshold, blank_threshold)` — optionally overrides `surya.settings` thresholds before constructing `DetectionPredictor`; call once per worker process
  - `extract_lines(page_image_path, model) -> dict` — opens image with PIL, runs `model([image])`, returns `{"lines": [...]}` with per-line `index`, `bbox`, `polygon`, `confidence`
  - `process_page_lines(page_record, model, target_storage, temp_dir) -> PageRecord` — resolves image from temp (fallback to target storage + local temp copy), calls `extract_lines`, serialises to JSON, writes to `{submission_id}/{image_basename}.json` on target, returns updated `PageRecord`
- `src/transcriptor_worker/config.py` — added `detector_text_threshold: float | None` and `detector_blank_threshold: float | None` fields, loaded from `DETECTOR_TEXT_THRESHOLD` / `DETECTOR_BLANK_THRESHOLD` env vars; `None` leaves library defaults unchanged

**Verification results:**
- `extract_lines` on a real page image: 12 lines detected, correct `index`/`bbox`/`polygon`/`confidence` fields ✓
- JSON written to `{submission_id}/{image_basename}.json` on target storage ✓
- Fallback path: temp file absent → image read from target storage, temp copy written then cleaned up after detection ✓
- Config threshold fields: `0.5`/`0.2` parsed correctly; unset → `None` ✓

### Phase 5 — COMPLETED

All steps done and verified:

- `src/transcriptor_worker/worker.py` — new module:
  - `StorageConfig` — picklable dataclass describing a storage backend (replaces passing boto3 clients across process boundaries)
  - `init_worker(text_threshold, blank_threshold)` — Pool initializer; sets up logging in the sub-process and calls `init_surya_model()` once, storing the predictor in a module-level global
  - `process_submission(submission, source_cfg, target_cfg, temp_dir)` — reconstructs storage backends, runs page extraction → line extraction → `desc.json` copy, returns `(SubmissionRecord, list[PageRecord])`
- `src/transcriptor_worker/coordinator.py` — `run()` fully implemented:
  - Builds source/target storage backends from config
  - Calls `build_work_queue()` (unchanged from Phase 2)
  - Carries forward existing completed rows from target manifests so the final CSV is a full picture
  - Spawns a `multiprocessing.Pool` using the **`"spawn"` context** (avoids fork+PyTorch deadlocks on macOS/Linux)
  - Uses `pool.imap_unordered` to collect `(SubmissionRecord, list[PageRecord])` results as they arrive
  - Writes final `submissions.csv` and `pages.csv` to target storage after all workers complete
  - For a local target, uses the target root directly as the temp dir (no extra copy step)
  - Cleans up any `mkdtemp` temp directory created for a remote target run

**Verification results:**
- End-to-end run (2 submissions, `WORKER_PARALLELISM=1`, local→local): both submissions `completed`, correct `submissions.csv` and `pages.csv` written ✓
- Per-submission output: `{submission_id}/0_p1.jpg`, `{submission_id}/0_p1.jpg.json` (12 and 25 lines respectively), `{submission_id}/desc.json` all present ✓
- JSON line records have correct keys (`index`, `bbox`, `polygon`, `confidence`) ✓
- Re-run: both submissions skipped immediately (work queue = 0), no Pool spawned ✓

### Phase 6 — COMPLETED

**Last updated:** 2026-06-12

All steps done and verified:

**Startup checks (`coordinator.py`):**
- `_check_source_readable()` — calls `list_prefixes` on source storage; raises `RuntimeError` with a descriptive message if it fails
- `_check_target_writable()` — writes a UUID-named probe object, then deletes it; raises `RuntimeError` if write fails
- Both checks run in `coordinator.run()` before any discovery or processing; `sys.exit(1)` on failure with a logged error
- `delete()` method added to `StorageBackend` ABC, `LocalStorageBackend`, and `S3StorageBackend` (idempotent in all implementations)

**Worker hardening (`worker.py`):**
- `init_worker()` wraps `init_surya_model()` in a `try/except`; model load failures set `_surya_model = None` and log an error instead of crashing the pool initializer; per-page exception handling in `process_submission` catches the resulting `AttributeError`

**Test suite (`tests/`):**
- `tests/__init__.py` — package marker
- `tests/fixtures/two_page.pdf` — 2-page fixture PDF generated with pyMuPDF
- `tests/fixtures/sample.jpg` — 50×50 white JPEG fixture
- `tests/test_config.py` — 23 tests: happy-path parsing, defaults, optional fields, all error paths (missing vars, bad types, bad floats, zero/negative parallelism, whitespace-only)
- `tests/test_storage_local.py` — 25 tests: all `LocalStorageBackend` methods (`list_prefixes`, `list_files`, `walk`, `read_bytes`, `read_text`, `write_bytes`, `write_text`, `exists`, `delete`), round-trip
- `tests/test_manifest.py` — 16 tests: empty write, single/multiple records, missing-file defaults, round-trip for both CSV types, comma-in-error survival
- `tests/test_discovery.py` — 10 tests: flat, nested, depth-mixed, scoped, empty, no-desc.json, extra-files
- `tests/test_pages.py` — 15 tests: 2-page PDF extraction, 1-based page numbers, filenames, target+temp writes, JPEG output validity, null mime-type fallback, image pass-through, error handling (missing desc.json, corrupted PDF, missing doc file, partial failure)
- `tests/test_image_transform.py` — 6 tests: no-op confirmed for JPEG, PNG, arbitrary, empty bytes, large payload, content unchanged

**Verification:** `uv run --extra dev pytest tests/ -v` → **94 passed, 0 failed** (5 cosmetic SWIG DeprecationWarnings from pymupdf, not from our code)

### Phase 7 — COMPLETED

**Last updated:** 2026-06-12

All steps done and verified:

- **`.env.example`** — already present (created by the user before Phase 7 started); reviewed and confirmed complete: covers all 13 env vars (`SOURCE_*`, `TARGET_*`, `WORKER_PARALLELISM`, `DETECTOR_TEXT_THRESHOLD`, `DETECTOR_BLANK_THRESHOLD`, `TEMP_DIR`) with local/S3 switch comments and inline documentation.

- **Dev dependencies** — already in `pyproject.toml` from Phase 1: `dev = ["pytest", "pytest-cov"]`.

- **Script entry point** — already wired in `pyproject.toml` from Phase 1: `transcriptor-worker = "transcriptor_worker.main:main"`.

- **Root `main.py` stub removed** — the leftover scaffold stub at the project root (`main.py: print("Hello from transcriptor-worker!")`) was deleted; the real entry point is `src/transcriptor_worker/main.py`.

- **pytest config added to `pyproject.toml`:**
  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  pythonpath = ["src"]
  ```
  `uv run --extra dev pytest` now works without specifying the test path or pythonpath manually.

**Verification:**
- `uv run --extra dev pytest` → **94 passed, 0 failed** ✓
- `uv run transcriptor-worker` (2-submission local smoke test):
  - Startup checks logged and passed ✓
  - 2 submissions discovered, both processed to `status=completed` ✓
  - `submissions.csv` and `pages.csv` written with correct content ✓
  - Page images and line JSON files present in each submission folder ✓
  - Re-run: work queue = 0, both submissions skipped immediately ✓
