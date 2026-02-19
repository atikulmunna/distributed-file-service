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
