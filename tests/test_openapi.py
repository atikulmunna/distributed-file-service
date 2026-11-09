from fastapi.testclient import TestClient

from app.main import app


def test_openapi_includes_standard_error_schema() -> None:
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()

    components = spec.get("components", {}).get("schemas", {})
    assert "ErrorResponse" in components

    init_responses = spec["paths"]["/v1/uploads/init"]["post"]["responses"]
    assert "401" in init_responses
    assert init_responses["401"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")


# The public API surface. A change here must be deliberate: update this list in the same
# commit that adds, removes, or renames an endpoint, and note it in the commit message.
EXPECTED_API_SURFACE = [
    ("/", "GET"),
    ("/console", "GET"),
    ("/health", "GET"),
    ("/metrics", "GET"),
    ("/ui", "GET"),
    ("/v1/admin/cleanup", "POST"),
    ("/v1/uploads/init", "POST"),
    ("/v1/uploads/{upload_id}/chunks/{chunk_index}", "PUT"),
    ("/v1/uploads/{upload_id}/complete", "POST"),
    ("/v1/uploads/{upload_id}/download", "GET"),
    ("/v1/uploads/{upload_id}/missing-chunks", "GET"),
    ("/version", "GET"),
]


def test_openapi_paths_unchanged() -> None:
    spec = app.openapi()
    surface = sorted((path, method.upper()) for path, operations in spec["paths"].items() for method in operations)
    assert surface == EXPECTED_API_SURFACE
