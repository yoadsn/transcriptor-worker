# transcriptor-worker

A data processing pipeline that ingests raw handwriting submissions (PDFs and images) from a source data store, extracts page images, runs Surya OCR layout detection to identify text lines, and writes the structured output — page images, per-line polygon JSON files, and CSV manifests — to a target data store. The output is designed to feed downstream bounding-box labeling systems used by human annotators.

## How it works

The pipeline is configured entirely through environment variables and supports both local filesystem and S3 storage backends with independent credentials per source and target. A coordinator process discovers submissions by recursively walking source storage for `desc.json` manifests, filters out already-completed submissions (incrementality), and dispatches pending work to a `multiprocessing.Pool` using the `spawn` context (required for PyTorch/Surya compatibility). Each worker process loads the Surya detection model once at startup and reuses it across all pages it handles.

For each submission, the worker rasterizes PDFs with pyMuPDF at 300 DPI, passes through image files as-is (with a no-op transformation hook for future deskewing/binarization), runs Surya line detection on every page, and copies the original `desc.json` to the target. Results are recorded in two CSV manifests — `submissions.csv` and `pages.csv` — that are carried forward across runs so that completed work is never reprocessed. Startup credential checks validate storage access before any real work begins, and per-submission error handling ensures a single failure never brings down the entire run.

## Running

```bash
cp .env.example .env   # then edit with your paths/credentials
uv run --env-file .env transcriptor-worker
```

See `planning/pipeline-impl.md` for the full implementation plan and `planning/pipeline-design.md` for the original design document.
