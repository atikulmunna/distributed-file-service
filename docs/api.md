# API

How an upload works, every endpoint, the error shape, and the API changes in this version. Links point to the tests that back each statement.

## How an upload works

1. **Start** with the file's name and size (and optionally its SHA-256). The server answers with an upload ID and the chunk layout.
2. **Send the chunks** in any order, several at a time. Each chunk carries its SHA-256 and is rejected if the bytes do not match ([test](../tests/test_api_flow.py)). Sending a chunk again is safe: the same content is a no-op ([test](../tests/test_chunk_races.py)).
3. **Resume** after any interruption: the server lists which chunks it is missing, and only those are sent again ([test](../tests/e2e/test_ui_smoke.py)).
4. **Complete.** The server assembles the parts. If a whole-file SHA-256 was declared, it answers `202 VERIFYING` and a background worker hashes the stored file; the upload becomes `COMPLETED` only if it matches, otherwise `FAILED` with the reason ([test](../tests/test_finalizer.py)).
5. **Download** with `Range` support, or create a short-lived download link that needs no API key ([test](../tests/test_download.py), [test](../tests/test_signed_urls.py)).

The same flow with curl (a 5 MiB chunk size; on S3 every chunk but the last must be at least 5 MiB):

```bash
API=http://localhost:8000 KEY=dev-key FILE=report.pdf
SIZE=$(stat -c %s "$FILE")
BODY=$(printf '{"file_name": "%s", "file_size": %d, "chunk_size": 5242880}' "$FILE" "$SIZE")

# 1. start (the Idempotency-Key makes a repeated request return the same upload)
UPLOAD=$(curl -s -X POST "$API/v1/uploads/init" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: init-$FILE-$SIZE" --data "$BODY" | python -c 'import json,sys; print(json.load(sys.stdin)["upload_id"])')

# 2. send each chunk with its SHA-256
split -b 5242880 -d -a 4 "$FILE" chunk-
for part in chunk-*; do
  index=$((10#${part#chunk-}))
  curl -s -X PUT "$API/v1/uploads/$UPLOAD/chunks/$index" -H "X-API-Key: $KEY" \
    -H "X-Chunk-SHA256: $(sha256sum "$part" | cut -d' ' -f1)" -H "Idempotency-Key: chunk-$UPLOAD-$index" \
    --data-binary @"$part"
done

# 3. complete, 4. download (or ask for a link)
curl -s -X POST "$API/v1/uploads/$UPLOAD/complete" -H "X-API-Key: $KEY"
curl -s "$API/v1/uploads/$UPLOAD/download" -H "X-API-Key: $KEY" -o "copy-of-$FILE"
curl -s -X POST "$API/v1/uploads/$UPLOAD/download-url" -H "X-API-Key: $KEY"
```

`scripts/load_test.py` does the same in Python, with parallel chunks and retries.

## Endpoints

Every `/v1` endpoint takes `X-API-Key` (or `Authorization: Bearer <JWT>` with `AUTH_MODE=jwt` or `hybrid`). The OpenAPI document is served at `/docs`; the public surface is pinned by a test ([test](../tests/test_openapi.py)).

| Method and path | What it does |
|---|---|
| `POST /v1/uploads/init` | Start an upload: `file_name`, `file_size`, optional `chunk_size` and `file_checksum_sha256`. `201` with the upload ID and layout. |
| `PUT /v1/uploads/{id}/chunks/{index}` | Send one chunk (`X-Chunk-SHA256`, `Idempotency-Key` optional). `202` once it is stored and recorded. |
| `POST /v1/uploads/{id}/complete` | Finish. `200 COMPLETED`, or `202 VERIFYING` when a whole-file checksum was declared. |
| `GET /v1/uploads/{id}` | Detail: status, chunk and byte counts, checksum result (`checksum_verified`), failure reason, timestamps. |
| `GET /v1/uploads/{id}/missing-chunks` | The chunk indexes the server has not received, for resuming. |
| `GET /v1/uploads/{id}/chunks?offset=&limit=` | Recorded chunks with size, SHA-256, status and storage attempts. |
| `GET /v1/uploads?status=&limit=&cursor=` | Your uploads, newest first, with keyset pagination. |
| `DELETE /v1/uploads/{id}` | Abort an unfinished upload and discard its stored data. |
| `GET /v1/uploads/{id}/download` | Download, with `Range` (`206`). |
| `POST /v1/uploads/{id}/download-url` | A link valid for `DOWNLOAD_URL_TTL_SECONDS` (300) that downloads without an API key. |
| `GET /v1/downloads/{token}` | Download by link, with `Range`. |
| `GET /v1/limits` | The bounds init enforces, so a client can choose a valid chunk size. |
| `GET /v1/stats` | This instance now and over the last 5 minutes (chunks, bytes, throttled requests, storage write p50 and p95). |
| `POST /v1/admin/cleanup` | Run cleanup now (admin users only). |
| `GET /health`, `GET /ready` | Liveness, and readiness (database, storage, queue) ([test](../tests/test_ready.py)). |
| `GET /metrics`, `GET /version` | Prometheus metrics (optionally behind `METRICS_TOKEN`), and the version. |

**Errors** share one shape: `{"detail", "error_code", "request_id", "upload_id", "trace_id"}`; `422` adds `errors` (field location, message, type; the submitted value is not echoed). A `5xx` carries only `detail`, `error_code` and `request_id`, and the cause is logged under that request ID ([test](../tests/test_secure_defaults.py)). `429` and `503` answers carry `Retry-After`.

**Ownership:** another user's upload answers `404`, exactly like a missing one, and idempotency keys are scoped to their owner ([test](../tests/test_secure_defaults.py)).

### Changes in this version

- `POST /v1/uploads/{id}/complete` returns `202` with status `VERIFYING` when a whole-file checksum was declared (verification moved to a background worker).
- An upload owned by another user returns `404` instead of `403`.
- `422` responses use the error shape above, with `errors` instead of FastAPI's list-valued `detail`.
- `Idempotency-Key` is limited to 128 characters, and init and complete keys are scoped per user (keys stored before this change no longer match).
- `GET /v1/uploads/{id}` returns the full detail; `upload_id`, `status` and `failure_reason` are unchanged.
- New: `GET /v1/limits`, `GET /v1/uploads`, `GET /v1/uploads/{id}/chunks`, `GET /v1/stats`, `POST /v1/uploads/{id}/download-url`, `GET /v1/downloads/{token}`.
