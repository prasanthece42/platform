# Team Payment API

A separate Python application for the first AegisAI shared-pipeline test. It keeps payment records in SQLite, validates amounts, and prevents duplicate creation when a request is retried. It does not contact a bank or charge money. It has no authentication and is intended for a local demo, not public payment processing.

## Run locally

Use Python 3.12 and `uv`:

```sh
uv venv --python 3.12 --seed .venv
.venv/bin/python -m pip install --require-hashes -r requirements-ci.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m uvicorn payments_api.main:app --host 127.0.0.1 --port 8020
```

Open http://127.0.0.1:8020/docs to try the API. Expand **POST /payments**, choose **Try it out**, enter an `Idempotency-Key` such as `order-001`, and submit:

```json
{"amount_cents": 2500, "currency": "CAD", "description": "First test order"}
```

`2500` means 25.00 CAD. The first request returns 201. Repeating the same key and details returns the same record with 200; changing the details for that key returns 409.

The API supports health checks, creating/listing/retrieving payment records, and cancelling records. Cancelling a record does not perform a refund. Records persist in `data/payments.db`; set `PAYMENTS_DATABASE_PATH` to use another SQLite file. The server runs as a single local process; persistent storage and authentication would need configuration for deployment.

## Verify

```sh
.venv/bin/python -m pytest tests --ignore=tests/integration --ignore=tests/contracts -q
.venv/bin/python -m pytest tests/integration -q
.venv/bin/python -m pytest tests/contracts -q
.venv/bin/python -m build --no-isolation
```

The root `pyproject.toml`, hash-locked `requirements-ci.txt` and executable tests match AegisAI's Python repository adapter. Integration and software contract tests enable its optional stages. These are software checks, not AI-model evaluations.

To refresh dependencies, use `uv pip compile pyproject.toml --extra ci --python-version 3.12 --universal --generate-hashes -o requirements-ci.txt` and review the resulting changes.

## GitHub pipeline

This directory is the entire team application; it has no dependency on AegisAI source or configuration. Publish these source files to the team's chosen repository, then connect that repository in AegisAI. The platform owner must publish/configure the central reusable pipeline once. AegisAI can then propose its pinned caller workflow through a pull request. Merge and activate that workflow before requesting a GitHub Actions run. The app does not include a fabricated central pipeline reference or a preconfigured deployment.

For this first pilot, this repository also publishes the reusable workflow and its reviewed `scripts/`, `runner/`, and `policies/` tooling. Other teams can reference that workflow at an immutable commit. These files are pipeline tooling, not application runtime dependencies.
