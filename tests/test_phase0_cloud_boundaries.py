from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from conftest import make_spec, synthetic_frame
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from skincare_advisor.api import create_app
from skincare_advisor.domain import (
    DateRange,
    InferenceSpec,
    MovingAverageConfig,
    RecommendationSpec,
    parse_run_spec,
)
from skincare_advisor.identity import IdentityContext


class HeaderIdentityProvider:
    def resolve(self, headers) -> IdentityContext:
        owner_id = headers.get("x-test-owner", "anonymous")
        return IdentityContext(
            owner_id=owner_id,
            subject=owner_id,
            authentication_mode="test",
        )


def test_api_isolates_catalog_aggregates_by_owner(runtime) -> None:
    settings, _, _ = runtime
    with TestClient(create_app(settings, HeaderIdentityProvider())) as client:
        created = client.post(
            "/api/catalogs/upload",
            headers={"x-test-owner": "alice"},
            data={"catalog_name": "Alice panel"},
            files={"file": ("panel.csv", synthetic_frame(30).to_csv(index=False), "text/csv")},
        )
        assert created.status_code == 200
        version_id = created.json()["id"]

        alice = client.get("/api/catalogs", headers={"x-test-owner": "alice"})
        bob = client.get("/api/catalogs", headers={"x-test-owner": "bob"})
        assert [item["name"] for item in alice.json()["catalogs"]] == ["Alice panel"]
        assert bob.json()["catalogs"] == []

        hidden = client.get(
            f"/api/catalog-versions/{version_id}",
            headers={"x-test-owner": "bob"},
        )
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "catalog_version_not_found"

        inbox = client.get("/api/inbox", headers={"x-test-owner": "alice"})
        assert inbox.status_code == 404
        assert inbox.json()["error"]["code"] == "local_endpoint_unavailable"


def test_api_rejects_client_resource_limits_above_server_policy(evidence) -> None:
    settings, _, _, version, mapping, profile = evidence
    restricted = settings.model_copy(update={"policy_max_rows": 100})
    spec = make_spec(version, mapping, profile)
    with TestClient(create_app(restricted)) as client:
        response = client.post("/api/specs", json=spec.model_dump(mode="json"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "resource_policy_exceeded"
    assert response.json()["error"]["detail"]["limits"]["max_rows"] == {
        "requested": 10_000,
        "allowed": 100,
    }


def test_recommendation_and_inference_contracts_are_explicit_and_discriminated() -> None:
    recommendation = RecommendationSpec(
        catalog_version_id="catalog-version",
        mapping_id="mapping",
        profile_id="profile",
        training_end=date(2026, 1, 11),
        recommendation=DateRange(start=date(2026, 1, 12), end=date(2026, 1, 18)),
        training_window_days=365,
        target_column="rating",
        models=(MovingAverageConfig(),),
        information_cutoff=datetime(2026, 1, 12, tzinfo=UTC),
    )
    inference = InferenceSpec(
        model_artifact_id="model-artifact",
        model_adapter_kind="neural_net",
        catalog_version_id="future-features",
        mapping_id="mapping",
        profile_id="profile",
        recommendation=DateRange(start=date(2026, 1, 12), end=date(2026, 1, 18)),
        target_column="rating",
        information_cutoff=datetime(2026, 1, 12, tzinfo=UTC),
    )

    assert recommendation.horizon_days == 7
    assert inference.horizon_days == 7
    assert parse_run_spec(recommendation.model_dump()).mode == "recommendation"
    assert parse_run_spec(inference.model_dump()).mode == "inference"


def test_legacy_database_upgrade_assigns_local_owner() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "legacy.sqlite3"
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path("src/skincare_advisor/migrations").resolve()),
        )
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
        command.upgrade(config, "0001_initial")
        engine = create_engine(f"sqlite:///{database}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO catalogs (id, name, created_at) "
                    "VALUES ('legacy-catalog', 'Legacy', CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "head")
        columns = {column["name"] for column in inspect(engine).get_columns("catalogs")}
        assert "owner_id" in columns
        with engine.connect() as connection:
            owner = connection.execute(
                text("SELECT owner_id FROM catalogs WHERE id='legacy-catalog'")
            ).scalar_one()
        assert owner == "local"
