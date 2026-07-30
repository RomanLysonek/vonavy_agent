const API_REQUEST_TIMEOUT_MS = 15_000;
const API_RETRY_DELAYS_MS = [250, 750];
const RETRYABLE_API_STATUSES = new Set([429, 502, 503, 504]);
const FORECAST_TERMINAL_STATUSES = new Set(["succeeded", "partial", "invalid", "failed"]);
const FORECAST_RUN_STORAGE_PREFIX = "vonavy_forecast_runs";
const ACCESS_TOKEN_REFRESH_SKEW_MS = 60_000;
let tokenRefreshPromise = null;

const state = {
  config: null,
  tokens: null,
  ownerId: null,
  validationResults: new Map(),
  sourceRevision: null,
};
const $ = (id) => document.getElementById(id);

function base64Url(bytes) {
  return btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

function randomVerifier() {
  const bytes = new Uint8Array(64);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

async function challenge(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

function decodeJwt(token) {
  const payload = token.split(".")[1].replaceAll("-", "+").replaceAll("_", "/");
  return JSON.parse(atob(payload.padEnd(Math.ceil(payload.length / 4) * 4, "=")));
}

function tokenExpiresSoon(token) {
  try {
    const expiresAt = Number(decodeJwt(token).exp) * 1000;
    return !Number.isFinite(expiresAt) || expiresAt <= Date.now() + ACCESS_TOKEN_REFRESH_SKEW_MS;
  } catch {
    return true;
  }
}
function saveTokens(tokens) {
  const existing = state.tokens && typeof state.tokens === "object" ? state.tokens : {};
  const next = { ...existing, ...tokens };
  if (typeof next.access_token !== "string" || !next.access_token) {
    throw new Error("Cognito returned no access token.");
  }
  if (typeof next.refresh_token !== "string" || !next.refresh_token) delete next.refresh_token;
  state.tokens = next;
  sessionStorage.setItem("vonavy_tokens", JSON.stringify(next));
}
function clearTokens() {
  sessionStorage.removeItem("vonavy_tokens");
  state.tokens = null;
  state.ownerId = null;
}
function showAuthenticationState() {
  const signedOut = $("signed-out");
  const signedIn = $("signed-in");
  if (!signedOut || !signedIn) return;
  signedOut.classList.toggle("hidden", Boolean(state.tokens));
  signedIn.classList.toggle("hidden", !state.tokens);
}

function loadTokens() {
  const raw = sessionStorage.getItem("vonavy_tokens");
  if (!raw) return null;
  try {
    const tokens = JSON.parse(raw);
    if (!tokens || typeof tokens !== "object" || typeof tokens.access_token !== "string") {
      throw new Error("Stored tokens are invalid.");
    }
    decodeJwt(tokens.access_token);
    if (tokenExpiresSoon(tokens.access_token) && !tokens.refresh_token) {
      sessionStorage.removeItem("vonavy_tokens");
      return null;
    }
    return tokens;
  } catch {
    sessionStorage.removeItem("vonavy_tokens");
    return null;
  }
}

function forecastRunStorageKey() {
  return state.ownerId ? `${FORECAST_RUN_STORAGE_PREFIX}:${state.ownerId}` : null;
}

function loadRememberedForecastRuns() {
  const key = forecastRunStorageKey();
  if (!key) return {};
  try {
    const value = JSON.parse(localStorage.getItem(key) || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return value;
  } catch {
    try {
      localStorage.removeItem(key);
    } catch {
      // Storage can be unavailable in privacy-restricted browser contexts.
    }
    return {};
  }
}

function saveRememberedForecastRuns(runs) {
  const key = forecastRunStorageKey();
  if (!key) return;
  try {
    if (Object.keys(runs).length) localStorage.setItem(key, JSON.stringify(runs));
    else localStorage.removeItem(key);
  } catch {
    // Forecast execution remains usable even when durable browser storage is unavailable.
  }
}

function rememberForecastRun(run) {
  const datasetId = run?.datasetId;
  const forecastRunId = run?.forecastRunId;
  const status = run?.links?.status;
  if (!datasetId || !forecastRunId || typeof status !== "string") return;
  const runs = loadRememberedForecastRuns();
  runs[datasetId] = {
    datasetId,
    forecastRunId,
    status: run.status,
    runMode: run.runMode || "single",
    adapterId: run.adapterId || null,
    adapterIds: Array.isArray(run.adapterIds) ? run.adapterIds : [],
    childRuns: Array.isArray(run.childRuns) ? run.childRuns : [],
    timeoutSeconds: Number(run.timeoutSeconds || 0),
    resultAvailable: Boolean(run.resultAvailable),
    links: {
      status,
      result: run.links?.result || null,
    },
    updatedAt: run.updatedAt || new Date().toISOString(),
  };
  saveRememberedForecastRuns(runs);
}
function rememberedForecastRun(datasetId) {
  return loadRememberedForecastRuns()[datasetId] || null;
}

function forgetForecastRun(datasetId) {
  const runs = loadRememberedForecastRuns();
  if (!(datasetId in runs)) return;
  delete runs[datasetId];
  saveRememberedForecastRuns(runs);
}

class ApiRequestError extends Error {
  constructor(message, { status = null, requestId = null, sourceRevision = null } = {}) {
    const references = [];
    if (requestId) references.push(`reference ${requestId}`);
    if (sourceRevision && sourceRevision !== "unknown") {
      references.push(`deployment ${sourceRevision.slice(0, 12)}`);
    }
    super(references.length ? `${message} (${references.join(", ")})` : message);
    this.name = "ApiRequestError";
    this.status = status;
    this.requestId = requestId;
    this.sourceRevision = sourceRevision;
  }
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
async function timedFetch(url, options) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), API_REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}
async function refreshAccessToken() {
  if (tokenRefreshPromise) return tokenRefreshPromise;
  const refreshToken = state.tokens?.refresh_token;
  if (typeof refreshToken !== "string" || !refreshToken) {
    clearTokens();
    showAuthenticationState();
    throw new ApiRequestError("Your sign-in session expired. Sign in again to continue.", {
      status: 401,
      sourceRevision: state.sourceRevision,
    });
  }
  tokenRefreshPromise = (async () => {
    const body = new URLSearchParams({
      grant_type: "refresh_token",
      client_id: state.config.userPoolClientId,
      refresh_token: refreshToken,
    });
    let response;
    try {
      response = await timedFetch(`${state.config.cognitoDomain}/oauth2/token`, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body,
      });
    } catch (error) {
      throw new ApiRequestError(
        error?.name === "AbortError"
          ? "Refreshing the sign-in session timed out."
          : "The sign-in session could not be refreshed.",
        { status: 401, sourceRevision: state.sourceRevision },
      );
    }
    const tokenResponse = await response.json().catch(() => ({}));
    if (!response.ok || typeof tokenResponse.access_token !== "string") {
      clearTokens();
      showAuthenticationState();
      throw new ApiRequestError("Your sign-in session expired. Sign in again to continue.", {
        status: 401,
        sourceRevision: state.sourceRevision,
      });
    }
    saveTokens({ ...tokenResponse, refresh_token: tokenResponse.refresh_token || refreshToken });
    return state.tokens.access_token;
  })();
  try {
    return await tokenRefreshPromise;
  } finally {
    tokenRefreshPromise = null;
  }
}
async function ensureFreshAccessToken(force = false) {
  const accessToken = state.tokens?.access_token;
  if (typeof accessToken !== "string" || !accessToken) return null;
  if (force || tokenExpiresSoon(accessToken)) return refreshAccessToken();
  return accessToken;
}

async function api(path, options = {}) {
  const baseHeaders = new Headers(options.headers || {});
  if (options.body && !baseHeaders.has("content-type")) {
    baseHeaders.set("content-type", "application/json");
  }

  const method = String(options.method || "GET").toUpperCase();
  const attempts = method === "GET" ? 3 : 1;
  let lastError = null;
  let authReplayAvailable = Boolean(state.tokens?.refresh_token);

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const headers = new Headers(baseHeaders);
    try {
      const accessToken = await ensureFreshAccessToken();
      if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);
      const execute = () =>
        timedFetch(`${state.config.apiBaseUrl}${path}`, {
          ...options,
          method,
          headers,
        });
      let response = await execute();
      if (response.status === 401 && authReplayAvailable) {
        authReplayAvailable = false;
        const refreshedToken = await ensureFreshAccessToken(true);
        headers.set("authorization", `Bearer ${refreshedToken}`);
        response = await execute();
      }
      const requestId = response.headers.get("x-vonavy-request-id");
      const sourceRevision = response.headers.get("x-vonavy-source-revision");
      if (sourceRevision) state.sourceRevision = sourceRevision;
      const payload = await response.json().catch(() => ({}));
      if (response.ok) return payload;
      if (response.status === 401) {
        clearTokens();
        showAuthenticationState();
        throw new ApiRequestError("Your sign-in session expired. Sign in again to continue.", {
          status: 401,
          requestId,
          sourceRevision,
        });
      }

      const error = new ApiRequestError(
        payload.error?.message || `Request failed (${response.status})`,
        { status: response.status, requestId, sourceRevision },
      );
      if (
        method === "GET" &&
        RETRYABLE_API_STATUSES.has(response.status) &&
        attempt < attempts - 1
      ) {
        lastError = error;
        await sleep(API_RETRY_DELAYS_MS[attempt]);
        continue;
      }
      throw error;
    } catch (error) {
      if (error instanceof ApiRequestError) throw error;
      const wrapped = new ApiRequestError(
        error?.name === "AbortError"
          ? "The request timed out."
          : "The service could not be reached.",
        { sourceRevision: state.sourceRevision },
      );
      if (method === "GET" && attempt < attempts - 1) {
        lastError = wrapped;
        await sleep(API_RETRY_DELAYS_MS[attempt]);
        continue;
      }
      throw wrapped;
    }
  }

  throw lastError || new ApiRequestError("The request could not be completed.");
}

async function login() {
  const verifier = randomVerifier();
  const oauthState = randomVerifier();
  sessionStorage.setItem("vonavy_pkce_verifier", verifier);
  sessionStorage.setItem("vonavy_oauth_state", oauthState);
  const params = new URLSearchParams({
    client_id: state.config.userPoolClientId,
    response_type: "code",
    redirect_uri: state.config.redirectUri,
    scope: state.config.scope,
    code_challenge_method: "S256",
    code_challenge: await challenge(verifier),
    state: oauthState,
  });
  location.assign(`${state.config.cognitoDomain}/oauth2/authorize?${params}`);
}

async function exchangeCode(code, returnedState) {
  const verifier = sessionStorage.getItem("vonavy_pkce_verifier");
  const expectedState = sessionStorage.getItem("vonavy_oauth_state");
  if (!verifier || !expectedState) throw new Error("The sign-in verifier is missing. Start sign-in again.");
  if (!returnedState || returnedState !== expectedState) {
    throw new Error("The sign-in response did not match this browser session.");
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: state.config.userPoolClientId,
    code,
    redirect_uri: state.config.redirectUri,
    code_verifier: verifier,
  });
  const response = await fetch(`${state.config.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) throw new Error("Cognito rejected the authorization code.");
  const tokenResponse = await response.json();
  if (!tokenResponse.access_token) throw new Error("Cognito returned no access token.");
  saveTokens(tokenResponse);
  sessionStorage.removeItem("vonavy_pkce_verifier");
  sessionStorage.removeItem("vonavy_oauth_state");
  history.replaceState({}, "", "/");
}

function logout() {
  clearTokens();
  const params = new URLSearchParams({
    client_id: state.config.userPoolClientId,
    logout_uri: state.config.redirectUri,
  });
  location.assign(`${state.config.cognitoDomain}/logout?${params}`);
}

async function upload(event) {
  event.preventDefault();
  const file = $("dataset-file").files[0];
  const name = $("dataset-name").value.trim();
  if (!file || !name) return;
  if (file.size > state.config.maximumUploadBytes) {
    $("status").textContent = `File exceeds the ${state.config.maximumUploadBytes.toLocaleString()} byte server limit.`;
    return;
  }
  const button = $("upload-button");
  button.disabled = true;
  $("status").textContent = "Creating a private upload session…";
  try {
    const mediaType = file.name.toLowerCase().endsWith(".parquet")
      ? "application/vnd.apache.parquet"
      : "text/csv";
    const session = await api("/api/upload-sessions", {
      method: "POST",
      body: JSON.stringify({
        datasetName: name,
        filename: file.name,
        mediaType,
        sizeBytes: file.size,
      }),
    });
    const form = new FormData();
    for (const [key, value] of Object.entries(session.upload.fields)) form.append(key, value);
    form.append("file", file);
    $("status").textContent = "Uploading directly to encrypted S3 storage…";
    const uploaded = await fetch(session.upload.url, { method: "POST", body: form });
    if (!uploaded.ok) throw new Error(`S3 upload failed (${uploaded.status})`);
    $("status").textContent = "Verifying immutable object version and declared size…";
    await api(`/api/upload-sessions/${session.uploadId}/complete`, {
      method: "POST",
      body: "{}",
    });
    $("status").textContent = "Upload complete. The dataset is ready for validation.";
    $("upload-form").reset();
    await listDatasets();
  } catch (error) {
    $("status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function validationMessage(job, result = null) {
  if (job.status === "succeeded" && result) {
    return `Validated ${result.row_count.toLocaleString()} rows and ${result.column_count.toLocaleString()} columns.`;
  }
  if (job.status === "invalid" && result) {
    const codes = result.validation_errors.map((issue) => issue.code).join(", ");
    return `Dataset is invalid: ${codes || "validation rules failed"}.`;
  }
  if (job.status === "failed") {
    return job.failure?.message || "Validation worker failed.";
  }
  return `Validation status: ${job.status}.`;
}

async function waitForValidation(job, output, button) {
  const terminal = new Set(["succeeded", "invalid", "failed"]);
  let current = job;
  const maxAttempts = Math.ceil((state.config.validationJobTimeoutSeconds + 600) / 3);
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    output.textContent = validationMessage(current);
    if (terminal.has(current.status)) {
      let result = null;
      if (current.resultAvailable) {
        result = await api(current.links.result);
      }
      if (current.status === "succeeded" && result) {
        state.validationResults.set(current.datasetId, {
          jobId: current.validationJobId,
          result,
        });
      }
      output.textContent = validationMessage(current, result);
      button.disabled = false;
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
    current = await api(current.links.status);
  }
  output.textContent = "Validation is still running. Refresh to check it again.";
  button.disabled = false;
}

async function validateDataset(dataset, output, button) {
  button.disabled = true;
  output.textContent = "Submitting an ephemeral CPU validation job…";
  try {
    const job = await api(`/api/datasets/${dataset.datasetId}/validations`, {
      method: "POST",
      body: JSON.stringify({ requestToken: crypto.randomUUID() }),
    });
    await waitForValidation(job, output, button);
  } catch (error) {
    output.textContent = error.message;
    button.disabled = false;
  }
}

function forecastMessage(run, result = null) {
  if (result?.schema_version === "forecast-comparison-result/v1") {
    const summary = result.summary || {};
    const requested = Number(summary.requested_models || result.adapter_ids?.length || 0);
    const succeeded = Number(summary.succeeded_models || 0);
    const best = summary.best_adapter_id
      ? ` Best comparable adapter: ${summary.best_adapter_id}.`
      : "";
    const limitation = summary.leaderboard_comparable === false && summary.comparison_reason
      ? ` ${summary.comparison_reason}`
      : "";
    return (
      `Model comparison ${result.status}: ${succeeded}/${requested} adapters ` +
      `succeeded.${best}${limitation}`
    );
  }
  if (run.status === "succeeded" && result) {
    const wape = result.holdout?.wape;
    const quality = typeof wape === "number" ? ` Holdout WAPE ${(wape * 100).toFixed(2)}%.` : "";
    return `Forecast complete: ${result.profile.entities * 7} rows.${quality}`;
  }
  if (run.status === "invalid" && result) {
    return result.failure?.message || "The forecast mapping or data is invalid.";
  }
  if (run.status === "partial") return "Model comparison completed with partial results.";
  if (run.status === "failed") return run.failure?.message || "Forecast worker failed.";
  if (run.runMode === "comparison") {
    const total = Array.isArray(run.adapterIds) ? run.adapterIds.length : 0;
    return `Model comparison status: ${run.status}${total ? ` (${total} adapters)` : ""}.`;
  }
  return `Forecast status: ${run.status}.`;
}
function promptColumn(label, fallback, optional = false) {
  const value = window.prompt(label, fallback);
  if (value === null) throw new Error("Forecast setup cancelled.");
  const clean = value.trim();
  if (!clean && !optional) throw new Error(`${label} is required.`);
  return clean || null;
}
function promptColumns(label, fallback = []) {
  const value = window.prompt(
    `${label} (comma separated, optional)`,
    fallback.join(", "),
  );
  if (value === null) throw new Error("Forecast setup cancelled.");
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function percentage(value) {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "unavailable";
}

function appendResultReview(output, result) {
  const review = result.review;
  if (!review) return;
  const details = document.createElement("details");
  details.className = "forecast-review";
  details.open = review.status === "needs_attention";
  const summary = document.createElement("summary");
  summary.textContent = `Result review: ${review.status.replaceAll("_", " ")}`;
  details.append(summary);

  const headline = document.createElement("p");
  headline.textContent = review.headline;
  details.append(headline);

  const evaluation = result.evaluation;
  if (evaluation) {
    const skill = evaluation.baseline_skill || {};
    const evidence = document.createElement("p");
    evidence.textContent =
      `Model WAPE ${percentage(skill.model_value)} vs seasonal baseline ` +
      `${percentage(skill.baseline_value)}; relative skill ` +
      `${percentage(skill.relative_improvement)}. Cold start ` +
      `${evaluation.cold_start_entity_count}/${evaluation.evaluated_entity_count}; ` +
      `feature extrapolation ${percentage(evaluation.feature_extrapolation_rate)}.`;
    details.append(evidence);
  }

  if (review.findings?.length) {
    const title = document.createElement("strong");
    title.textContent = "Findings";
    details.append(title);
    const list = document.createElement("ul");
    for (const finding of review.findings) {
      const item = document.createElement("li");
      item.textContent = `[${finding.severity}] ${finding.message}`;
      list.append(item);
    }
    details.append(list);
  }

  if (review.recommendations?.length) {
    const title = document.createElement("strong");
    title.textContent = "Measured next experiments";
    details.append(title);
    const list = document.createElement("ol");
    for (const recommendation of review.recommendations) {
      const item = document.createElement("li");
      item.textContent = recommendation.action;
      list.append(item);
    }
    details.append(list);
  }
  output.append(document.createTextNode(" "), details);
}

function appendComparisonLeaderboard(output, result) {
  const leaderboard = Array.isArray(result.leaderboard) ? result.leaderboard : [];
  if (!leaderboard.length) return;
  const details = document.createElement("details");
  details.open = true;
  const heading = document.createElement("summary");
  heading.textContent = result.summary?.leaderboard_comparable
    ? "Common-evidence model leaderboard"
    : "Model results (common leaderboard unavailable)";
  details.append(heading);
  const table = document.createElement("table");
  table.className = "comparison-leaderboard";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["Rank", "Model", "Status", "Holdout WAPE", "Rows"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  for (const entry of leaderboard) {
    const row = document.createElement("tr");
    const values = [
      entry.rank ?? "—",
      entry.adapter_id,
      entry.status,
      typeof entry.holdout_wape === "number"
        ? `${(entry.holdout_wape * 100).toFixed(2)}%`
        : "unavailable",
      Number(entry.forecast_rows || 0).toLocaleString(),
    ];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.append(cell);
    }
    body.append(row);
  }
  table.append(body);
  details.append(table);
  output.append(document.createTextNode(" "), details);
}
function appendDownloads(output, downloads, prefix = "") {
  for (const [name, url] of Object.entries(downloads || {})) {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = `Download ${prefix}${name}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "artifact-link";
    output.append(document.createTextNode(" "), link);
  }
}
function appendComparisonModels(output, result) {
  for (const model of result.models || []) {
    const adapter = model.adapter?.id || "model";
    const details = document.createElement("details");
    details.className = "comparison-model";
    const summary = document.createElement("summary");
    const wape = model.holdout?.wape;
    summary.textContent = `${adapter}: ${model.status}${
      typeof wape === "number" ? ` · WAPE ${(wape * 100).toFixed(2)}%` : ""
    }`;
    details.append(summary);
    const body = document.createElement("div");
    if (model.failure?.message) {
      const failure = document.createElement("p");
      failure.textContent = model.failure.message;
      body.append(failure);
    }
    appendResultReview(body, model);
    appendDownloads(body, model.downloads, `${adapter} `);
    details.append(body);
    output.append(document.createTextNode(" "), details);
  }
}
function showForecastResult(output, run, result) {
  output.replaceChildren(document.createTextNode(forecastMessage(run, result)));
  appendResultReview(output, result);
  if (result.schema_version === "forecast-comparison-result/v1") {
    appendComparisonLeaderboard(output, result);
    appendComparisonModels(output, result);
  } else {
    appendDownloads(output, result.downloads);
  }
  if (["succeeded", "partial"].includes(run.status)) {
    const discuss = document.createElement("button");
    discuss.type = "button";
    discuss.className = "secondary";
    discuss.textContent = "Discuss result with AI";
    discuss.addEventListener("click", () => {
      reviewForecastResult(run, output, discuss);
    });
    output.append(document.createTextNode(" "), discuss);
  }
}
async function waitForForecast(run, output, button) {
  let current = run;
  rememberForecastRun(current);
  const requestedModels = Math.max(1, Number(current.adapterIds?.length || 1));
  const configuredTimeout = Number(current.timeoutSeconds || 0);
  const timeoutSeconds = configuredTimeout > 0
    ? configuredTimeout
    : state.config.forecastJobTimeoutSeconds * requestedModels;
  const maxAttempts = Math.ceil((timeoutSeconds + 900) / 3);
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    output.textContent = forecastMessage(current);
    if (FORECAST_TERMINAL_STATUSES.has(current.status)) {
      if (current.resultAvailable) {
        const result = await api(current.links.result);
        showForecastResult(output, current, result);
      }
      button.disabled = false;
      return;
    }
    await sleep(3000);
    current = await api(current.links.status);
    rememberForecastRun(current);
  }
  output.textContent = "Forecast is still running. Refresh to restore its current state.";
  button.disabled = false;
}
async function restoreForecast(dataset, output, button) {
  const remembered = rememberedForecastRun(dataset.datasetId);
  if (!remembered?.links?.status) return;

  button.disabled = true;
  output.textContent = "Restoring the latest forecast…";
  try {
    const run = await api(remembered.links.status);
    rememberForecastRun(run);
    if (FORECAST_TERMINAL_STATUSES.has(run.status)) {
      if (run.resultAvailable) {
        const result = await api(run.links.result);
        showForecastResult(output, run, result);
      } else {
        output.textContent = forecastMessage(run);
      }
      button.disabled = false;
      return;
    }
    await waitForForecast(run, output, button);
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 404) {
      forgetForecastRun(dataset.datasetId);
      output.textContent = "";
    } else {
      output.textContent = `Latest forecast could not be restored: ${error.message}`;
    }
    button.disabled = false;
  }
}

let agentContext = null;
const AGENT_TURN_POLL_INTERVAL_MS = 1200;
const AGENT_TURN_MAX_POLLS = 300;

function appendInlineMarkdown(root, text) {
  const source = String(text || "");
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^)\s]+\))/g;
  let offset = 0;
  for (const match of source.matchAll(pattern)) {
    if (match.index > offset) root.append(document.createTextNode(source.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      root.append(code);
    } else if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      root.append(strong);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      emphasis.textContent = token.slice(1, -1);
      root.append(emphasis);
    } else {
      const parts = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (!parts) {
        root.append(document.createTextNode(token));
      } else {
        let safe = null;
        try {
          const target = new URL(parts[2], location.origin);
          if (target.protocol === "https:" || target.origin === location.origin) safe = target;
        } catch {
          safe = null;
        }
        if (!safe) {
          root.append(document.createTextNode(parts[1]));
        } else {
          const link = document.createElement("a");
          link.textContent = parts[1];
          link.href = safe.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          root.append(link);
        }
      }
    }
    offset = match.index + token.length;
  }
  if (offset < source.length) root.append(document.createTextNode(source.slice(offset)));
}

function markdownCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
}

function renderSafeMarkdown(root, markdown) {
  root.replaceChildren();
  const lines = String(markdown || "").replaceAll("\r\n", "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (line.trim().startsWith("```")) {
      const language = line.trim().slice(3).trim();
      const values = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        values.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (language) code.dataset.language = language.slice(0, 32);
      code.textContent = values.join("\n");
      pre.append(code);
      root.append(pre);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const title = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(title, heading[2]);
      root.append(title);
      index += 1;
      continue;
    }
    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1])
    ) {
      const headers = markdownCells(line);
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      for (const value of headers) {
        const cell = document.createElement("th");
        appendInlineMarkdown(cell, value);
        headRow.append(cell);
      }
      head.append(headRow);
      table.append(head);
      const body = document.createElement("tbody");
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const row = document.createElement("tr");
        for (const value of markdownCells(lines[index]).slice(0, headers.length)) {
          const cell = document.createElement("td");
          appendInlineMarkdown(cell, value);
          row.append(cell);
        }
        body.append(row);
        index += 1;
      }
      table.append(body);
      const scroller = document.createElement("div");
      scroller.className = "agent-markdown-table";
      scroller.append(table);
      root.append(scroller);
      continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      const list = document.createElement(unordered ? "ul" : "ol");
      const matcher = unordered ? /^\s*[-*+]\s+(.+)$/ : /^\s*\d+[.)]\s+(.+)$/;
      while (index < lines.length) {
        const itemMatch = lines[index].match(matcher);
        if (!itemMatch) break;
        const item = document.createElement("li");
        appendInlineMarkdown(item, itemMatch[1]);
        list.append(item);
        index += 1;
      }
      root.append(list);
      continue;
    }
    if (line.startsWith("> ")) {
      const quote = document.createElement("blockquote");
      const values = [];
      while (index < lines.length && lines[index].startsWith("> ")) {
        values.push(lines[index].slice(2));
        index += 1;
      }
      appendInlineMarkdown(quote, values.join(" "));
      root.append(quote);
      continue;
    }
    const paragraphLines = [line.trim()];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      !/^\s*([-*+]\s+|\d+[.)]\s+|>\s+)/.test(lines[index]) &&
      !lines[index].trim().startsWith("```") &&
      !(lines[index].includes("|") && index + 1 < lines.length && /-{3,}/.test(lines[index + 1]))
    ) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    const paragraph = document.createElement("p");
    appendInlineMarkdown(paragraph, paragraphLines.join(" "));
    root.append(paragraph);
  }
}

function appendAgentMessage(role, text) {
  const item = document.createElement("article");
  item.className = `agent-message ${role}`;
  if (role === "assistant") renderSafeMarkdown(item, text);
  else item.textContent = text;
  $("agent-messages").append(item);
  item.scrollIntoView({ block: "end" });
}

function agentMaximumTurns(session = null) {
  const configured = Number(
    session?.maximumTurns ?? state.config?.forecastAgentMaximumTurns ?? 20,
  );
  return Number.isInteger(configured) && configured > 0 ? configured : 20;
}
function showAgentPane(name) {
  if (!agentContext) return;
  const showPlan = name === "plan" && agentContext.phase === "planning" && agentContext.plan;
  agentContext.planVisible = Boolean(showPlan);
  $("agent-messages").classList.toggle("hidden", Boolean(showPlan));
  $("agent-plan").classList.toggle("hidden", !showPlan);
  $("agent-plan-toggle").setAttribute("aria-pressed", String(Boolean(showPlan)));
}
function toggleAgentPlan() {
  if (!agentContext?.plan || agentContext.phase !== "planning") return;
  showAgentPane(agentContext.planVisible ? "chat" : "plan");
}
function setAgentPlan(plan) {
  if (!agentContext) return;
  const planning = agentContext.phase === "planning";
  agentContext.plan = planning ? plan || null : null;
  const confirm = $("agent-confirm");
  const toggle = $("agent-plan-toggle");
  confirm.classList.toggle("hidden", !planning);
  toggle.classList.toggle("hidden", !planning || !plan);
  if (!planning || !plan) {
    $("agent-plan-summary").replaceChildren();
    confirm.disabled = true;
    showAgentPane("chat");
    return;
  }
  renderAgentPlan(plan);
  confirm.disabled = false;
  showAgentPane("chat");
}
function appendPreprocessingPlan(root, item) {
  const preprocessing = item?.plan || item;
  if (!preprocessing) return;
  const adapterLabel = item?.adapter?.label || item?.adapterId || "Model";
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const operations = Array.isArray(preprocessing.operations) ? preprocessing.operations : [];
  summary.textContent = `Preprocessing: ${operations.length} fixed operations · ${adapterLabel}`;
  details.append(summary);
  const metadata = document.createElement("p");
  const digest = preprocessing.digest?.value || "unavailable";
  metadata.textContent = `Catalogue: ${preprocessing.catalogVersion} · plan digest: ${digest.slice(0, 12)}…`;
  details.append(metadata);
  const review = preprocessing.review || {};
  const findings = Array.isArray(preprocessing.findings) ? preprocessing.findings : [];
  const attentionFindingLabel = findings.length === 1 ? "attention finding" : "attention findings";
  const reviewLine = document.createElement("p");
  reviewLine.textContent = `Preprocessing review: ${review.status || "unavailable"} · max severity: ${review.maxSeverity || "unavailable"} · ${findings.length} ${attentionFindingLabel}`;
  details.append(reviewLine);
  const attentionFindings = findings.filter((finding) => finding.severity === "warning");
  if (attentionFindings.length) {
    const findingsList = document.createElement("ul");
    for (const finding of attentionFindings) {
      const listItem = document.createElement("li");
      listItem.textContent = `${finding.severity}: ${finding.message}`;
      findingsList.append(listItem);
    }
    details.append(findingsList);
  }
  const list = document.createElement("ol");
  for (const operation of operations) {
    const listItem = document.createElement("li");
    listItem.textContent = `${operation.action} (${operation.status})`;
    list.append(listItem);
  }
  details.append(list);
  root.append(details);
}
function renderAgentPlan(plan) {
  const root = $("agent-plan-summary");
  root.replaceChildren();
  const adapters = Array.isArray(plan.adapters) && plan.adapters.length
    ? plan.adapters
    : plan.adapter
      ? [{ id: plan.adapterId, ...plan.adapter }]
      : [];
  const lines = [
    plan.summary,
    `${plan.executionMode === "comparison" ? "Models" : "Model"}: ${
      adapters.map((adapter) => adapter.label || adapter.id).join(", ")
    }`,
    `Target: ${plan.mapping.targetColumn}`,
    `Timestamp: ${plan.mapping.timestampColumn}`,
    `Entity: ${plan.mapping.entityColumn || "single series"}`,
    `Training end: ${plan.trainingEnd}`,
    `Forecast: ${plan.forecastStart} through ${plan.forecastEnd}`,
  ];
  if (plan.executionMode === "comparison") {
    lines.push("Execution: one bounded sequential comparison job; results appear after every model is terminal.");
  }
  if (plan.warnings?.length) lines.push(`Warnings: ${plan.warnings.join("; ")}`);
  for (const line of lines.filter(Boolean)) {
    const paragraph = document.createElement("p");
    paragraph.textContent = line;
    root.append(paragraph);
  }
  const preprocessingPlans = Array.isArray(plan.preprocessingPlans) && plan.preprocessingPlans.length
    ? plan.preprocessingPlans
    : plan.preprocessingPlan
      ? [{ adapterId: plan.adapterId, adapter: plan.adapter, plan: plan.preprocessingPlan }]
      : [];
  for (const preprocessing of preprocessingPlans) appendPreprocessingPlan(root, preprocessing);
}
function configureAgentDialog(phase) {
  const planning = phase === "planning";
  $("agent-plan-toggle").classList.toggle("hidden", !planning || !agentContext?.plan);
  $("agent-confirm").classList.toggle("hidden", !planning);
  showAgentPane("chat");
}
async function agenticForecastDataset(dataset, output, button) {
  const validation = state.validationResults.get(dataset.datasetId);
  if (!validation?.jobId) {
    output.textContent = "Validate this dataset first so the agent can inspect its safe profile.";
    return;
  }
  button.disabled = true;
  agentContext = {
    phase: "planning",
    dataset,
    output,
    button,
    validation,
    session: null,
    plan: null,
    planVisible: false,
    running: false,
    pending: false,
  };
  $("agent-messages").replaceChildren();
  $("agent-status").textContent = `0 of ${agentMaximumTurns()} turns`;
  $("agent-input").value = "";
  $("agent-input").disabled = false;
  $("agent-send").disabled = false;
  setAgentPlan(null);
  configureAgentDialog("planning");
  appendAgentMessage(
    "assistant",
    `Tell me the forecasting objective. You have up to ${agentMaximumTurns()} turns. If you ask to run multiple models, the confirmable plan will list every adapter and execute a real all-terminal comparison.`,
  );
  $("agent-dialog").showModal();
  $("agent-input").focus();
}
function reviewForecastResult(run, output, button) {
  button.disabled = true;
  agentContext = {
    phase: "result",
    run,
    output,
    button,
    session: null,
    plan: null,
    planVisible: false,
    running: false,
    pending: false,
  };
  $("agent-messages").replaceChildren();
  $("agent-status").textContent = `0 of ${agentMaximumTurns()} turns`;
  $("agent-input").value = "";
  $("agent-input").disabled = false;
  $("agent-send").disabled = false;
  setAgentPlan(null);
  configureAgentDialog("result");
  appendAgentMessage(
    "assistant",
    `Ask me about this completed ${run.runMode === "comparison" ? "model comparison" : "forecast"} for up to ${agentMaximumTurns()} turns. I can explain measured evidence and practical implications, but I cannot rerun or modify it.`,
  );
  $("agent-dialog").showModal();
  $("agent-input").focus();
}
async function waitForAgentTurn(session) {
  let current = session;
  for (let poll = 0; poll < AGENT_TURN_MAX_POLLS; poll += 1) {
    if (!current.turn?.pending) return current;
    const label = current.turn.status === "queued" ? "queued" : "working";
    $("agent-status").textContent = `Agent turn ${label}…`;
    await sleep(AGENT_TURN_POLL_INTERVAL_MS);
    current = await api(current.links.self);
    if (agentContext) agentContext.session = current;
  }
  throw new Error("The agent turn did not finish within six minutes.");
}

async function sendAgentMessage(event) {
  event.preventDefault();
  if (!agentContext || agentContext.pending || agentContext.running) return;
  const input = $("agent-input");
  const message = input.value.trim();
  if (!message) return;
  appendAgentMessage("user", message);
  input.value = "";
  input.disabled = true;
  $("agent-send").disabled = true;
  agentContext.pending = true;
  if (agentContext.phase === "planning") setAgentPlan(null);
  $("agent-status").textContent = "Queueing the agent turn…";
  try {
    const requestToken = crypto.randomUUID();
    let queued;
    if (agentContext.session) {
      queued = await api(agentContext.session.links.messages, {
        method: "POST",
        body: JSON.stringify({ message, requestToken }),
      });
    } else if (agentContext.phase === "result") {
      queued = await api(`/api/forecasts/${agentContext.run.forecastRunId}/agent/sessions`, {
        method: "POST",
        body: JSON.stringify({ message, requestToken }),
      });
    } else {
      queued = await api(
        `/api/datasets/${agentContext.dataset.datasetId}/forecast-agent/sessions`,
        {
          method: "POST",
          body: JSON.stringify({
            validationJobId: agentContext.validation.jobId,
            message,
            requestToken,
          }),
        },
      );
    }
    agentContext.session = queued;
    const session = await waitForAgentTurn(queued);
    agentContext.session = session;
    if (session.turn?.status === "failed") {
      const failure = session.turn.error?.message || "The agent turn could not be completed.";
      appendAgentMessage("assistant", `I could not complete that turn: ${failure}`);
      $("agent-status").textContent = "Agent turn failed. You can try again.";
      return;
    }
    if (session.message) appendAgentMessage("assistant", session.message);
    if (agentContext.phase === "planning" && session.draftPlan) {
      setAgentPlan(session.draftPlan);
    }
    const maximumTurns = agentMaximumTurns(session);
    if (session.draftPlan) {
      $("agent-status").textContent = "Review the plan or continue chatting to revise it.";
    } else if (session.turnCount >= maximumTurns) {
      $("agent-status").textContent = `${maximumTurns}-turn conversation complete.`;
    } else {
      $("agent-status").textContent = `Turn ${session.turnCount} of ${maximumTurns}`;
    }
  } catch (error) {
    appendAgentMessage("assistant", `I could not complete that turn: ${error.message}`);
    $("agent-status").textContent = "Agent turn failed. You can try again.";
  } finally {
    if (agentContext) {
      agentContext.pending = false;
      const complete =
        Number(agentContext.session?.turnCount || 0) >= agentMaximumTurns(agentContext.session);
      input.disabled = complete;
      $("agent-send").disabled = complete;
      if (!complete) input.focus();
    }
  }
}
async function confirmAgentPlan() {
  if (!agentContext || agentContext.phase !== "planning") return;
  if (agentContext.pending) {
    $("agent-status").textContent = "Wait for the current agent turn to finish.";
    return;
  }
  if (agentContext.running) {
    $("agent-status").textContent = "The confirmed workflow is already being submitted.";
    return;
  }
  if (!agentContext.plan) {
    $("agent-status").textContent = "Ask the agent to prepare a confirmable plan first.";
    appendAgentMessage("assistant", "A confirmable plan has not been created yet.");
    return;
  }
  const plan = agentContext.plan;
  const adapterIds = Array.isArray(plan.adapterIds) && plan.adapterIds.length
    ? plan.adapterIds
    : [plan.adapterId];
  const labels = Array.isArray(plan.adapters) && plan.adapters.length
    ? plan.adapters.map((adapter) => adapter.label || adapter.id)
    : [plan.adapter?.label || plan.adapterId];
  const preprocessingPlans = (
    Array.isArray(plan.preprocessingPlans) && plan.preprocessingPlans.length
  )
    ? plan.preprocessingPlans.map((item) => item.plan)
    : [plan.preprocessingPlan].filter(Boolean);
  const preprocessingDigest = preprocessingPlans[0]?.digest?.value || "unavailable";
  const operationCount = preprocessingPlans.reduce(
    (total, preprocessing) => total + (
      Array.isArray(preprocessing?.operations)
        ? preprocessing.operations.length
        : 0
    ),
    0,
  );
  const attentionCount = preprocessingPlans.reduce(
    (total, preprocessing) => total + (
      Array.isArray(preprocessing?.review?.attentionFindingIds)
        ? preprocessing.review.attentionFindingIds.length
        : 0
    ),
    0,
  );
  const approved = window.confirm(
    `${plan.summary}\n\n${adapterIds.length > 1 ? "Models" : "Model"}: ${labels.join(", ")}\n` +
      `Target: ${plan.mapping.targetColumn}\nTraining end: ${plan.trainingEnd}\n` +
      `Forecast: ${plan.forecastStart} through ${plan.forecastEnd}\n` +
      `Preprocessing: ${operationCount} fixed operations across ` +
      `${preprocessingPlans.length} plan(s) · ` +
      `${preprocessingDigest.slice(0, 12)}…\n` +
      `Attention findings: ${attentionCount}\n\n` +
      (adapterIds.length > 1
        ? "Confirm this immutable comparison. All listed models will run " +
          "sequentially in one bounded job, and the final result will appear only " +
          "after every model is terminal."
        : "Confirm this immutable plan and start the scale-to-zero workflow?"),
  );
  if (!approved) return;
  agentContext.running = true;
  $("agent-confirm").disabled = true;
  $("agent-plan-toggle").disabled = true;
  $("agent-status").textContent = adapterIds.length > 1
    ? `Submitting ${adapterIds.length}-model comparison…`
    : `Submitting ${labels[0]}…`;
  try {
    const request = {
      requestToken: crypto.randomUUID(),
      trainingEnd: plan.trainingEnd,
      mapping: plan.mapping,
    };
    if (adapterIds.length > 1) request.adapterIds = adapterIds;
    else request.adapterId = adapterIds[0];
    const run = await api(`/api/datasets/${agentContext.dataset.datasetId}/forecasts`, {
      method: "POST",
      body: JSON.stringify(request),
    });
    const { output, button } = agentContext;
    $("agent-dialog").close();
    output.textContent = adapterIds.length > 1
      ? `Submitted ${adapterIds.length}-model comparison after explicit confirmation.`
      : `Submitted ${labels[0]} after explicit confirmation.`;
    await waitForForecast(run, output, button);
    agentContext = null;
  } catch (error) {
    appendAgentMessage("assistant", `The confirmed workflow could not start: ${error.message}`);
    $("agent-status").textContent = "Submission failed.";
    $("agent-confirm").disabled = false;
    $("agent-plan-toggle").disabled = false;
    agentContext.running = false;
  }
}
function closeAgentDialog() {
  if (agentContext && !agentContext.running && agentContext.button) {
    agentContext.button.disabled = false;
  }
  $("agent-dialog").close();
  agentContext = null;
}
async function forecastDataset(dataset, output, button) {
  button.disabled = true;
  try {
    const validation = state.validationResults.get(dataset.datasetId);
    if (!validation?.jobId) {
      throw new Error("Validate this dataset first so the AI can inspect its safe profile.");
    }
    const objective = window.prompt(
      "What should the forecast prioritize? (optional)",
      "Forecast the next seven days of demand using known future context.",
    );
    if (objective === null) throw new Error("Forecast setup cancelled.");
    output.textContent = "Asking the AI to prepare a leakage-safe forecast plan…";
    const plan = await api(`/api/datasets/${dataset.datasetId}/forecast-agent`, {
      method: "POST",
      body: JSON.stringify({
        validationJobId: validation.jobId,
        objective: objective.trim(),
      }),
    });
    const suggested = plan.mapping;
    const modelChoice = window.prompt(
      "Choose model: xgboost, neuralnet, or chronos",
      "chronos",
    );
    if (modelChoice === null) throw new Error("Forecast setup cancelled.");
    const normalisedModel = modelChoice.trim().toLowerCase();
    const adapterId = normalisedModel === "neuralnet"
      ? "neuralnet-direct-v1"
      : normalisedModel === "xgboost"
        ? "xgboost-direct-v1"
        : normalisedModel === "chronos"
          ? "chronos2-zero-shot-v1"
          : null;
    if (!adapterId) throw new Error("Model must be xgboost, neuralnet, or chronos.");
    const modelLabel = adapterId === "neuralnet-direct-v1"
      ? "Best NeuralNet"
      : adapterId === "chronos2-zero-shot-v1"
        ? "Chronos-2 Zero-shot"
        : "Quick XGBoost";
    const mapping = {
      timestampColumn: promptColumn("Timestamp column", suggested.timestampColumn),
      entityColumn: promptColumn(
        "Entity/product column (optional)",
        suggested.entityColumn || "",
        true,
      ),
      targetColumn: promptColumn("Target column", suggested.targetColumn),
      availabilityColumn: promptColumn(
        "Availability column (optional)",
        suggested.availabilityColumn || "",
        true,
      ),
      knownFutureNumeric: promptColumns(
        "Known-future numeric columns",
        suggested.knownFutureNumeric,
      ),
      knownFutureCategorical: promptColumns(
        "Known-future categorical columns",
        suggested.knownFutureCategorical,
      ),
      staticNumeric: promptColumns("Static numeric columns", suggested.staticNumeric),
      staticCategorical: promptColumns(
        "Static categorical columns",
        suggested.staticCategorical,
      ),
      excluded: suggested.excluded,
    };
    const trainingEnd = promptColumn(
      "Confirm the last observed training date (YYYY-MM-DD)",
      plan.trainingEnd,
    );
    const warnings = plan.warnings.length
      ? `\nWarnings:\n- ${plan.warnings.join("\n- ")}`
      : "";
    const approved = window.confirm(
      `${plan.summary}\n\n` +
        `Mode: ${plan.agentMode}${plan.model ? ` / ${plan.model}` : ""}\n` +
        `Target: ${mapping.targetColumn}\n` +
        `Timestamp: ${mapping.timestampColumn}\n` +
        `Entity: ${mapping.entityColumn || "single series"}\n` +
        `Training end: ${trainingEnd}\n` +
        `Forecast: ${plan.forecastStart} through ${plan.forecastEnd}\n` +
        "Privacy: only validated profile metadata was sent; no raw rows or raw string values." +
        `${warnings}\n\nStart ${modelLabel} on the scale-to-zero worker?`,
    );
    if (!approved) throw new Error("Forecast plan was not confirmed.");
    output.textContent = `Submitting the confirmed ${modelLabel} forecast plan…`;
    const run = await api(`/api/datasets/${dataset.datasetId}/forecasts`, {
      method: "POST",
      body: JSON.stringify({
        requestToken: crypto.randomUUID(),
        adapterId,
        trainingEnd,
        mapping,
      }),
    });
    await waitForForecast(run, output, button);
  } catch (error) {
    output.textContent = error.message;
    button.disabled = false;
  }
}
async function listDatasets() {
  const payload = await api("/api/datasets");
  const root = $("datasets");
  root.replaceChildren();
  if (!payload.datasets.length) {
    root.textContent = "No datasets uploaded yet.";
    return;
  }
  for (const dataset of payload.datasets) {
    const item = document.createElement("article");
    item.className = "dataset";
    const title = document.createElement("strong");
    title.textContent = dataset.name;
    const meta = document.createElement("span");
    meta.textContent = `${dataset.filename} · ${dataset.status} · ${dataset.sizeBytes.toLocaleString()} bytes`;
    const actions = document.createElement("div");
    actions.className = "dataset-actions";
    const validationStatus = document.createElement("span");
    validationStatus.className = "validation-status";
    if (dataset.status === "uploaded") {
      const validateButton = document.createElement("button");
      validateButton.type = "button";
      validateButton.className = "secondary";
      validateButton.textContent = "Validate dataset";
      validateButton.addEventListener("click", () => {
        validateDataset(dataset, validationStatus, validateButton);
      });
      actions.append(validateButton);
      const forecastButton = document.createElement("button");
      forecastButton.type = "button";
      forecastButton.className = "secondary";
      forecastButton.textContent = "AI → Forecast";
      forecastButton.addEventListener("click", () => {
        agenticForecastDataset(dataset, validationStatus, forecastButton);
      });
      actions.append(forecastButton, validationStatus);
      restoreForecast(dataset, validationStatus, forecastButton);
    }
    item.append(title, meta, actions);
    root.append(item);
  }
}

async function start() {
  state.config = await fetch("/config.json", { cache: "no-store" }).then((response) => {
    if (!response.ok) throw new Error("Application configuration is unavailable.");
    return response.json();
  });
  $("upload-policy").textContent = `Server policy: up to ${state.config.maximumUploadBytes.toLocaleString()} bytes per file and ${state.config.maximumDatasetsPerOwner} retained dataset slots per account.`;
  const query = new URLSearchParams(location.search);
  const code = query.get("code");
  if (code) await exchangeCode(code, query.get("state"));
  state.tokens = loadTokens();
  if (state.tokens) {
    try {
      await ensureFreshAccessToken();
    } catch (error) {
      if (!(error instanceof ApiRequestError) || error.status !== 401) throw error;
    }
  }
  showAuthenticationState();
  if (state.tokens) {
    const claims = decodeJwt(state.tokens.access_token);
    state.ownerId = claims.sub;
    $("identity").textContent = claims.username || claims.sub;
    await listDatasets();
  }
}

$("login").addEventListener("click", login);
$("logout").addEventListener("click", logout);
$("upload-form").addEventListener("submit", upload);
$("agent-form").addEventListener("submit", sendAgentMessage);
$("agent-confirm").addEventListener("click", confirmAgentPlan);
$("agent-plan-toggle").addEventListener("click", toggleAgentPlan);
$("agent-close").addEventListener("click", closeAgentDialog);
$("agent-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeAgentDialog();
});
$("refresh").addEventListener("click", () => {
  listDatasets().catch((error) => {
    $("status").textContent = error.message;
  });
});
start().catch((error) => {
  $("signed-out").classList.remove("hidden");
  $("signed-out").querySelector("p").textContent = error.message;
});
