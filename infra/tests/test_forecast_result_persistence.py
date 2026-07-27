from pathlib import Path

ROOT = Path(__file__).parents[1]
WEB_APP = ROOT / "web" / "app.js"


def test_forecast_runs_are_persisted_per_authenticated_owner() -> None:
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'const FORECAST_RUN_STORAGE_PREFIX = "vonavy_forecast_runs";' in source
    assert "ownerId: null" in source
    assert "state.ownerId = claims.sub;" in source
    start = source[source.index("async function start()") :]
    assert start.index("state.ownerId = claims.sub;") < start.index("await listDatasets();")
    assert "`${FORECAST_RUN_STORAGE_PREFIX}:${state.ownerId}`" in source
    assert "localStorage.getItem(key)" in source
    assert "localStorage.setItem(key, JSON.stringify(runs))" in source
    assert "rememberForecastRun(current);" in source


def test_dataset_list_restores_terminal_and_running_forecasts() -> None:
    source = WEB_APP.read_text(encoding="utf-8")

    assert "async function restoreForecast(dataset, output, button)" in source
    assert "const run = await api(remembered.links.status);" in source
    assert "FORECAST_TERMINAL_STATUSES.has(run.status)" in source
    assert "const result = await api(run.links.result);" in source
    assert "await waitForForecast(run, output, button);" in source
    assert "restoreForecast(dataset, validationStatus, forecastButton);" in source
    assert "error instanceof ApiRequestError && error.status === 404" in source
    assert "forgetForecastRun(dataset.datasetId);" in source


def test_forecast_downloads_keep_the_application_open() -> None:
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'link.target = "_blank";' in source
    assert 'link.rel = "noopener noreferrer";' in source
