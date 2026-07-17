from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from vonavy_agent.domain import OriginSpec


@dataclass(frozen=True)
class ExpectedCell:
    role: str
    origin: date
    horizon: int
    entity: str
    forecast_date: date
    row_indices: tuple[int, ...]
    observation_available: bool | None
    included: bool


def expected_grid(
    entities: pd.Series,
    dates: pd.Series,
    observation_available: pd.Series,
    origins: tuple[OriginSpec, ...],
    horizon_days: int,
    scoring_policy: str,
) -> tuple[ExpectedCell, ...]:
    entity_values = sorted(str(value) for value in entities.dropna().unique())
    cells: list[ExpectedCell] = []
    for origin in origins:
        for horizon in range(1, horizon_days + 1):
            forecast_date = date.fromordinal(origin.date.toordinal() + horizon - 1)
            forecast_timestamp = pd.Timestamp(forecast_date)
            for entity in entity_values:
                mask = (entities == entity) & (dates == forecast_timestamp)
                indices = tuple(int(value) for value in entities.index[mask])
                observed: bool | None = None
                if len(indices) == 1:
                    raw = observation_available.loc[indices[0]]
                    if not pd.isna(raw):
                        observed = bool(raw)
                included = not (scoring_policy == "available_only" and observed is False)
                cells.append(
                    ExpectedCell(
                        role=origin.role,
                        origin=origin.date,
                        horizon=horizon,
                        entity=entity,
                        forecast_date=forecast_date,
                        row_indices=indices,
                        observation_available=observed,
                        included=included,
                    )
                )
    return tuple(cells)
