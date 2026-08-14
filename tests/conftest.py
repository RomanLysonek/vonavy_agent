from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy.engine import Engine

from skincare_advisor.api import migrate
from skincare_advisor.catalogs import CatalogRegistry, build_profile
from skincare_advisor.domain import (
    AvailabilityKind,
    AvailabilityPolicy,
    CatalogMappingSpec,
    DateRange,
    ExperimentSpec,
    FeatureMapping,
    FeatureRole,
    MovingAverageConfig,
    OriginSpec,
    ResourceLimits,
    RidgeDirectConfig,
    SeasonalNaiveConfig,
)
from skincare_advisor.experiments import create_experiment_spec
from skincare_advisor.persistence import (
    CatalogMapping,
    CatalogVersion,
    DataProfile,
    create_db_engine,
)
from skincare_advisor.settings import Settings


def synthetic_frame(days: int = 120, *, duplicate: bool = False) -> pd.DataFrame:
    start = date(2025, 1, 1)
    rows: list[dict[str, Any]] = []
    for entity_index, entity in enumerate(("product_id-a", "product_id-b")):
        for offset in range(days):
            day = start + timedelta(days=offset)
            skin_type_match = int((offset + entity_index) % 13 == 0)
            rows.append(
                {
                    "date": day.isoformat(),
                    "product_id": entity,
                    "rating": float(
                        100 + 20 * entity_index + day.weekday() * 2 + skin_type_match * 15
                    ),
                    "skin_type_match": skin_type_match,
                    "brand": "north" if entity_index == 0 else "south",
                }
            )
    if duplicate:
        rows.append(dict(rows[0]))
    return pd.DataFrame(rows)


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[Settings, Engine, CatalogRegistry]:
    settings = Settings(managed_root=tmp_path / "state", supervise_worker=False)
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    return settings, engine, CatalogRegistry(settings, engine)


@pytest.fixture
def evidence(
    runtime: tuple[Settings, Engine, CatalogRegistry],
) -> tuple[Settings, Engine, CatalogRegistry, CatalogVersion, CatalogMapping, DataProfile]:
    settings, engine, registry = runtime
    content = synthetic_frame().to_csv(index=False).encode()
    version = registry.ingest_stream(io.BytesIO(content), "panel.csv", "Panel")
    mapping = registry.create_mapping(
        version.id,
        CatalogMappingSpec(
            timestamp_column="date",
            entity_column="product_id",
            target_column="rating",
            target_availability=AvailabilityPolicy(kind=AvailabilityKind.EVENT_TIME),
            features=(
                FeatureMapping(
                    name="skin_type_match",
                    role=FeatureRole.KNOWN_FUTURE,
                    availability=AvailabilityPolicy(kind=AvailabilityKind.ORIGIN),
                ),
                FeatureMapping(
                    name="brand",
                    role=FeatureRole.STATIC,
                    availability=AvailabilityPolicy(kind=AvailabilityKind.ALWAYS),
                ),
            ),
        ),
    )
    profile = build_profile(registry, version.id, mapping.id, 10)
    return settings, engine, registry, version, mapping, profile


def make_spec(
    version: CatalogVersion,
    mapping: CatalogMapping,
    profile: DataProfile,
    *,
    models: tuple[SeasonalNaiveConfig | MovingAverageConfig | RidgeDirectConfig, ...] | None = None,
) -> ExperimentSpec:
    return ExperimentSpec(
        catalog_version_id=version.id,
        mapping_id=mapping.id,
        profile_id=profile.id,
        train=DateRange(start=date(2025, 1, 1), end=date(2025, 3, 10)),
        calibration=DateRange(start=date(2025, 3, 11), end=date(2025, 3, 25)),
        test=DateRange(start=date(2025, 3, 26), end=date(2025, 4, 10)),
        origins=(
            OriginSpec(date=date(2025, 3, 11), role="calibration"),
            OriginSpec(date=date(2025, 3, 26), role="test"),
        ),
        horizon_days=2,
        training_window_days=60,
        entity_column="product_id",
        target_column="rating",
        features=CatalogMappingSpec.model_validate_json(mapping.canonical_json).features,
        models=models
        or (
            SeasonalNaiveConfig(),
            MovingAverageConfig(window_days=28),
            RidgeDirectConfig(),
        ),
        seeds=(42,),
        evaluation_as_of=datetime(2025, 5, 2, tzinfo=UTC),
        minimum_coverage=1.0,
        resources=ResourceLimits(
            max_rows=10_000,
            max_entities=10,
            max_origins=10,
            max_models=3,
            wall_seconds=120,
            memory_mb=1024,
        ),
    )


@pytest.fixture
def spec_row(
    evidence: tuple[Settings, Engine, CatalogRegistry, CatalogVersion, CatalogMapping, DataProfile],
):
    _, engine, _, version, mapping, profile = evidence
    return create_experiment_spec(engine, make_spec(version, mapping, profile))
