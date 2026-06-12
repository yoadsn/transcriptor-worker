# Handwriting OCR — Data Processing Pipeline Design

## Overview

This document describes the design of a data processing pipeline that ingests raw handwriting submission data from a source data store and produces a structured, labeling-ready dataset in a target data store. The output feeds a downstream bounding-box labeling system used by human annotators.

The pipeline operates entirely against file storage — source, target, and local temporary storage on the worker host. There is no database involved. State is maintained through flat CSV manifests and the presence or absence of output files on the target storage.

This document does not cover the labeling system itself.

---

## Source Data

### Structure

The source data consists of **submissions**. Each submission is made by a user and contains one or more **docs**. A doc is either:

- A single image of a handwritten page (JPEG or other image format), or
- A multi-page PDF of scanned handwritten pages (which may combine pages from multiple physical documents).

### Storage Layout

Source data is stored in an S3 bucket (or local filesystem for development). Submissions reside in leaf folders at an arbitrary nesting depth. The pipeline discovers submissions by recursively searching for `desc.json` files anywhere in the source hierarchy.

**The name of the leaf folder is the submission ID** and must be globally unique.

Each submission folder contains:

- `desc.json` — metadata about the contributing user and an enumeration of the doc files in this submission.
- The doc files themselves (JPEG, PDF, or other image formats). Only files listed in `desc.json` are treated as docs; other files in the folder are ignored.

### desc.json

`desc.json` is the authoritative manifest for a submission. The pipeline follows it to identify which files to process. Its full contents are copied as-is to the target storage for consumers to reference.

---

## Target Data

The target stores all pipeline outputs. It requires no database. Instead, consumers interact with two flat CSV manifests and a set of per-submission folders, each containing page images, extracted line JSON files, and the original `desc.json`.

### Storage Layout

```
<target-root>/
  submissions.csv
  pages.csv
  <submission_id>/
    desc.json
    <page_image_filename>
    <page_image_basename>.json
    ...
```

Each submission folder is **one level deep** — there is no recursive nesting on the target. All page images and their corresponding line JSON files for a submission sit directly inside the submission folder.

### submissions.csv

One row per submission across all pipeline runs. Completed submissions from prior runs are carried forward verbatim.

| Column | Description |
|---|---|
| `submission_id` | The submission folder name from the source |
| `status` | `completed` or `failed` |
| `error` | Error message if `status` is `failed`, empty otherwise |

### pages.csv

One row per page across all submissions and all pipeline runs. Pages from prior completed submissions are carried forward verbatim.

| Column | Description |
|---|---|
| `submission_id` | The submission this page belongs to |
| `doc_filename` | The source doc file this page was extracted from (e.g., `scan.pdf`) |
| `page_number` | 1-based page index within the doc |
| `status` | `completed` or `failed` |
| `error` | Error message if `status` is `failed`, empty otherwise |
| `image_filename` | Filename of the page image within the submission folder; empty if `failed` |
| `lines_filename` | Filename of the lines JSON file within the submission folder; empty if `failed` |

`image_filename` and `lines_filename` are plain filenames, not full paths. A consumer resolves them as `<submission_id>/<image_filename>` relative to the target root.

### Per-Page Line JSON

For each successfully processed page, a JSON file is written alongside the page image. The file shares the same base name as the image file, with a `.json` extension (e.g., `abc123.jpg` → `abc123.json`).

The JSON file contains the array of line polygons extracted by Surya for that page. Its schema is determined by Surya's output format.

### How Consumers Use the Target Dataset

1. Download `submissions.csv` to enumerate all available submissions and their statuses.
2. Download `pages.csv` to enumerate all pages, their source docs, and their file locations.
3. For each page of interest, resolve the image and lines files as `<target-root>/<submission_id>/<image_filename>` and `<target-root>/<submission_id>/<lines_filename>`.
4. Access the original submission metadata via `<target-root>/<submission_id>/desc.json`.

Consumers are expected to poll the manifests periodically and ingest new or updated rows into their own data stores as needed. They access stored images directly from the target storage.

---

## Pipeline

### Stages

#### Stage 1 — Source Discovery

The coordinator process recursively walks the source storage, searching for all `desc.json` files at any nesting depth. Each discovered file represents one submission. The result is an in-memory list of all submission IDs and their source paths.

#### Stage 2 — Load Target Manifest and Filter

The coordinator downloads the existing `submissions.csv` from the target storage, if it exists. Any submission with `status = completed` in the existing manifest is excluded from processing — its rows are carried forward as-is into the new manifest. Submissions absent from the manifest, or present with `status = failed`, are queued for (re-)processing in full.

> **Recoverability:** Recovery granularity is at the submission level. If a submission did not complete cleanly in a prior run, the entire submission is reprocessed. Within a run, the only recoverability mechanism is the worker's local working copies of the CSV manifests, which are updated progressively as work completes and uploaded to the target at the very end of the run.

#### Stage 3 — Page Extraction

For each submission to be processed, a page extraction worker:

1. Reads each doc file listed in `desc.json`.
2. For image files, uses the image as-is (no post-processing in this version; a future image improvement stage is anticipated here).
3. For PDF files, rasterizes each page to an image using **ImageMagick** (or equivalent).
4. Derives a filename for each page from the doc filename and page number (e.g., `<doc_basename>_p<N>.jpg`).
5. Writes the page image to the target storage under `<submission_id>/<page_image_filename>`.
6. Also writes the image to a path under the OS temp directory for local reuse by the downstream line extraction stage. This temp storage is not managed by the worker; the OS is responsible for cleanup.
7. Appends a row to the local working copy of `pages.csv` for each extracted page.

If page extraction fails for a submission, the submission is recorded as `failed` in the local working copy of `submissions.csv` with an error message, and the run continues with the next submission.

#### Stage 4 — Line Extraction

For each successfully extracted page, a line extraction worker:

1. Loads the page image from local temp storage if available; falls back to the target storage.
2. Runs layout detection using **Surya** to extract line polygons.
3. Writes the resulting JSON to the target storage under `<submission_id>/<page_image_basename>.json`.
4. Updates the corresponding row in the local working copy of `pages.csv` to `status = completed` with `image_filename` and `lines_filename` populated.

If line extraction fails for a page, that page's row in the local `pages.csv` is marked `status = failed` with an error message. No JSON file is written for that page. The submission continues processing its remaining pages.

When all pages for a submission have been processed (regardless of individual page outcomes), the submission is recorded as `status = completed` in the local working copy of `submissions.csv`. A submission is considered completed even if some of its pages failed; page-level failures are visible in `pages.csv`.

The line extraction version is **hard-coded** in the worker build. To run a new extraction version, the worker code must be updated. Since completed submissions are skipped entirely on re-runs, extracting new versions for already-processed submissions would require a targeted mechanism outside the current scope (see Future Work).

#### Stage 5 — Copy submission desc.json

For each processed submission, the worker copies `desc.json` from the source submission folder to `<submission_id>/desc.json` on the target storage.

#### Stage 6 — Manifest Upload

Once all submissions have been processed, the coordinator uploads the final local working copies of `submissions.csv` and `pages.csv` to the target storage root, replacing the previous versions. The manifests at this point contain all rows carried forward from prior runs plus all rows produced in the current run.

---

## System Design

### Configuration

The worker is configured entirely through environment variables.

| Variable | Description |
|---|---|
| `SOURCE_STORAGE_TYPE` | `s3` or `local` |
| `SOURCE_STORAGE_PATH` | S3 URI (e.g., `s3://bucket/prefix`) or local path |
| `SOURCE_AWS_ACCESS_KEY_ID` | AWS access key (S3 source only) |
| `SOURCE_AWS_SECRET_ACCESS_KEY` | AWS secret key (S3 source only) |
| `SOURCE_AWS_REGION` | AWS region (S3 source only) |
| `TARGET_STORAGE_TYPE` | `s3` or `local` |
| `TARGET_STORAGE_PATH` | S3 URI or local path |
| `TARGET_AWS_ACCESS_KEY_ID` | AWS access key (S3 target only) |
| `TARGET_AWS_SECRET_ACCESS_KEY` | AWS secret key (S3 target only) |
| `TARGET_AWS_REGION` | AWS region (S3 target only) |
| `WORKER_PARALLELISM` | Maximum number of parallel sub-processes (default: 4) |

### Startup Checks

Before processing begins, the worker validates:

- All required environment variables are present and well-formed.
- Read credentials for the source storage are valid (by performing a lightweight list or head operation).
- Write credentials for the target storage are valid (by performing a lightweight put or head operation).

If any check fails, the worker exits immediately with a descriptive error. No partial processing occurs.

### Worker Architecture

The worker runs as a single coordinator process that dispatches work to a pool of sub-processes using Python's native `multiprocessing` module. Queues are held in-memory using `multiprocessing.Queue`. There is no persistent queue; the coordinator builds the work queue from source discovery and the existing target manifest at the start of each run.

```
┌──────────────────────────────────────────┐
│             Coordinator Process           │
│                                          │
│  1. Source discovery                     │
│  2. Load target manifest, filter         │
│  3. Dispatch to Page Extraction Pool     │
│  4. Dispatch to Line Extraction Pool     │
│  5. Copy desc.json files                 │
│  6. Upload manifests to target           │
└──────────┬───────────────┬───────────────┘
           │               │
  ┌────────▼──────┐  ┌─────▼───────────────┐
  │  Page         │  │  Line               │
  │  Extraction   │  │  Extraction Pool    │
  │  Pool         │  │  (N processes)      │
  │  (N processes)│  │                     │
  │               │  │  - Preloads Surya   │
  │  - ImageMagick│  │    model on startup │
  │  - Target     │  │  - Target storage   │
  │    storage    │  │    writes           │
  │    writes     │  │  - Local CSV        │
  │  - Local CSV  │  │    updates          │
  │    updates    │  │                     │
  └───────────────┘  └─────────────────────┘
```

**Sub-processes write page images and line JSON files directly to the target storage** and update the local working copies of the CSV manifests. The coordinator does not relay results; it dispatches jobs and waits for completion signals before proceeding to the manifest upload stage.

**Parallelism** is process-based for all stages. This avoids GIL contention issues with ML libraries (Surya) and provides clean fault isolation between jobs.

Each line extraction process **preloads the Surya model once at startup** and reuses it across all jobs it handles, avoiding per-job model loading overhead.

### Local Working State

During a run, the coordinator maintains two CSV files in the OS temp directory:

- A working `submissions.csv`, pre-populated with carried-forward rows from the existing target manifest.
- A working `pages.csv`, pre-populated with carried-forward rows from the existing target manifest.

Sub-processes append to or update these files as they complete jobs. At the end of the run, the coordinator uploads these files to the target storage root. If the worker process is interrupted before the upload stage, the target manifests retain their previous state from the last successful run; no partial results are published.

### Retry Policy

I/O operations that write to storage (network or disk) are retried up to **3 times** with brief back-off before the failure is surfaced. The pipeline does not retry at the job level; a failed submission or page is recorded as failed in the local manifest and the run continues.

### Scheduling

The worker is designed to be run on a regular schedule (e.g., via cron or a job scheduler). Each run reconstructs all necessary state from the source storage and the existing target manifest at startup.

---

## Technology Stack

| Concern | Choice |
|---|---|
| Language | Python |
| Object storage client | Boto3 |
| PDF/image rasterization | ImageMagick (via subprocess) |
| Layout / line detection | Surya |
| Inter-process queues | Python `multiprocessing.Queue` |

---

## Future Work

The following items are explicitly out of scope for this version and are noted here to guide future iterations:

- **Image post-processing** — A no-op stage placeholder exists in the page extraction step where image improvements (deskewing, binarization, contrast enhancement, resampling) will be added in a future version.
- **Delta manifest updates** — Currently, the full manifest is rewritten on every run. As the dataset grows, a delta mechanism that appends only new rows will be needed to keep run times and upload costs manageable.
- **Re-extraction of completed submissions** — Currently, completed submissions are unconditionally skipped. A future mechanism (e.g., a forced re-run flag or a version-based skip policy) would allow re-extracting lines for already-completed submissions when the extraction model is updated.
- **HTML status report** — A per-run HTML report summarizing submission statuses, page counts, and errors, pushed to target storage for operator review.
- **Doc-level recovery** — Currently, recovery granularity is at the submission level. A future version could support re-running a single doc within a submission.
