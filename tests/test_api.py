from __future__ import annotations

from conftest import synthetic_frame
from fastapi.testclient import TestClient

from skincare_advisor.api import create_app


def test_api_health_upload_and_typed_error(runtime) -> None:
    settings, _, _ = runtime
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        response = client.post(
            "/api/catalogs/upload",
            data={"catalog_name": "API panel"},
            files={"file": ("panel.csv", synthetic_frame(30).to_csv(index=False), "text/csv")},
        )
        assert response.status_code == 200
        assert response.json()["row_count"] == 60
        missing = client.get("/api/catalog-versions/not-found")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "catalog_version_not_found"
