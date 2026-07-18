# Experiment Agent

> Current development line: Phase 0 cloud-neutral boundaries. Local evaluation remains fully supported; AWS deployment is not implemented yet.

Local, deterministic forecasting experiment workbench for the NOTINO interview
portfolio. It copies permitted CSV/Parquet data into immutable content-addressed
storage, profiles and maps availability, blocks leakage before execution, runs
fast daily panel baselines in a separate worker process, compares common-row
evidence, and exports a static report.

The application needs no cloud service, API key, database server, sibling
checkout, or internet connection after dependencies are installed.

## Five-minute demo

Requires Python 3.11 or 3.12 and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run vonavy-agent demo-data .vonavy-agent/inbox/demo-demand.csv
uv run vonavy-agent serve
```

Open <http://127.0.0.1:8765> and:

1. Upload `.vonavy-agent/inbox/demo-demand.csv` as `Interview demand demo`.
2. Keep the suggested mapping: `date`, `store`, `demand`; target available at
   event time; `promotion` known at origin and `region` static.
3. Save the mapping/profile. Review the generated train, calibration, and test
   ranges.
4. Create the spec. The gate must be visible and pass before Run is enabled.
5. Confirm the exact gate, run the three baselines, compare calibration versus
   audit evidence, then export the selected run.

The downloaded ZIP contains `index.html`, `report.json`, and `manifest.json`.
Open `index.html` directly; it has no external assets or server dependency.

## Data contract

The first version supports regular daily panel demand:

- one timestamp column;
- one optional entity column;
- a numeric target;
- explicit target information-known-at and feature availability policies;
- an optional, separate product/observation-availability boolean plus an
  explicit `assume_available`, `available_only`, or `require_available` scoring
  policy;
- feature roles `past_only`, `known_future`, `static`, and `excluded`.

For daily aggregates, `available_at_event_time` means the value becomes eligible
at the next day's origin; the current day's target is never visible to its own
forecast.

An ingest creates a logical dataset version and never changes its source.
Snapshot and append modes materialise new immutable Parquet content. Browser
uploads and direct children of the configured local inbox are the only ingest
surfaces. URL fetches, traversal, symlinks, recursive watching, and automatic
experiment execution are not supported.

## Evidence contract

- Calibration evidence may guide development selection. Test evidence is
  labelled audit-only.
- Every rolling origin fits preprocessing and the model only on data available
  at that origin.
- Same-weekday seasonal naive, moving average, and direct Ridge are scored on
  the same entity/date/origin/horizon rows.
- WAPE with a zero actual denominator is unsupported, not reported as zero.
- Missing dates, values, availability proof, or minimum history block the run;
  the engine never silently imputes or drops them.
- Anomaly data without truth labels is described as exceedance/alert rate, never
  false-alarm rate.
- Chronos remains an optional manifest-described challenger. This slice does not
  run inference, training, or fine-tuning.

## Runtime and operations

By default state lives under `.vonavy-agent/` and Uvicorn binds to
`127.0.0.1:8765`. Override settings with the `VONAVY_AGENT_` prefix, for example:

```bash
VONAVY_AGENT_MANAGED_ROOT=/safe/local/path uv run vonavy-agent serve
```

The web process only enqueues jobs. A supervised worker claims SQLite leases and
launches each experiment through an argv-array Python subprocess with
`shell=False`, generation-fenced ownership, bounded concurrent log draining,
one-thread numerical settings, timeout/cancellation, a short atomic publication
state, process-group reap, and parent-death monitoring. Expired leases are
recovered during every claim cycle. To manage processes separately:

```bash
VONAVY_AGENT_SUPERVISE_WORKER=false uv run vonavy-agent serve
uv run vonavy-agent worker
```

Useful commands:

```bash
uv run vonavy-agent migrate
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Managed artifacts

Each run publishes canonical spec, gate, profile, predictions, metrics,
environment, logs, and a hash manifest. Provenance includes Git commit/tree/
dirty state, dependency and evidence hashes, environment, seeds, safe command,
runtime, limits, warnings, errors, and output hashes. The source fingerprint
hashes tracked and untracked executed files while excluding ignored runtime
artifacts. Unavailable provenance is represented explicitly rather than
fabricated.

SQLite remains the local metadata adapter. Local mode resolves every request to
one explicit owner named `local`; the API and persistence layer are now
owner-scoped so a future Cognito adapter can provide real multi-user isolation.
The server also enforces trusted resource ceilings above the resource envelope
requested by a client specification.

The domain distinguishes historical `EvaluationSpec`, unseen-future
`ForecastSpec`, and stored-model `InferenceSpec`. Only evaluation is executable
in this local slice. AWS storage, metadata, identity, and Batch implementations
will be added behind the interfaces in `vonavy_agent.ports`; they are not
simulated by the current local runner. See `docs/phase-0-cloud-boundaries.md`.

Airflow, Celery, Redis, Kubernetes, arbitrary shell, uploaded code execution,
and editable sibling imports remain non-goals.
