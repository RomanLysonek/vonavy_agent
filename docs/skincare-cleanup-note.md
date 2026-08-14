# Skincare advisor cleanup note

## Cleanup performed

- Renamed the Python package, CLI entry point, environment prefix, local managed root, and infrastructure package from the old agent name to `skincare_advisor` / `skincare-advisor` / `SKINCARE_ADVISOR_`.
- Reworked local UI copy, demo data defaults, README, contracts, and tests toward skincare product recommendation terminology: catalog versions, product IDs, ratings, skin-type match, brand, and recommendation contracts.
- Removed tracked references to the old Vonavy/forecast/Chronos/weather vocabulary from source, tests, docs, and infrastructure code, excluding generic AWS phrases such as “On-Demand”.
- Preserved the local ingest/storage path as a catalog loader backed by immutable Parquet content, SQLite metadata, Alembic migrations, and the existing leakage/availability gate.
- Updated the AWS control-plane slice names and configuration prefixes without deploying, bootstrapping, or starting any crawl.

## Intentional legacy-shaped infrastructure still present

- The local recommendation engine still uses a temporal evaluation/backtest architecture (`EvaluationSpec`, rolling origins, horizons, calibration/test splits, WAPE/MAE/RMSE) because that is the working, tested scoring core. It is now framed as skincare recommendation evidence rather than an old forecasting product.
- The dormant optional adapter interfaces for anomaly diagnostics and optional model challengers remain as manifest-described extension points; they are not executed by the local slice unless explicitly wired and confirmed.
- The `infra/` CDK control-plane scaffold remains present but undeployed. It is retained as future infrastructure, not active runtime.
- No tracked Notino scraper or separate DB-loader module existed on this branch during cleanup; no untracked crawler/scraper artifacts were removed. The preserved loader is the catalog ingest + SQLite/Alembic path in `skincare_advisor.catalogs` and `skincare_advisor.persistence`.
- `.github/workflows/ci.yml` is intentionally left unchanged because the available GitHub token lacks `workflow` scope; changing workflow files is blocked by GitHub until credentials are refreshed.

## Verification

- `git grep` over tracked source/docs/tests found no old Vonavy/forecast/Chronos/weather/domain-demand residue after cleanup, aside from the unchanged GitHub workflow file and generic AWS “On-Demand” wording in ops notes.
- Root and infrastructure test/lint/type gates pass locally.
