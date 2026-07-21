const state = { config: null, tokens: null, validationResults: new Map() };
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

function saveTokens(tokens) {
  state.tokens = tokens;
  sessionStorage.setItem("vonavy_tokens", JSON.stringify(tokens));
}

function loadTokens() {
  const raw = sessionStorage.getItem("vonavy_tokens");
  if (!raw) return null;
  try {
    const tokens = JSON.parse(raw);
    const claims = decodeJwt(tokens.access_token);
    if (claims.exp * 1000 <= Date.now() + 30_000) {
      sessionStorage.removeItem("vonavy_tokens");
      return null;
    }
    return tokens;
  } catch {
    sessionStorage.removeItem("vonavy_tokens");
    return null;
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.tokens) headers.set("authorization", `Bearer ${state.tokens.access_token}`);
  if (options.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  const response = await fetch(`${state.config.apiBaseUrl}${path}`, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `Request failed (${response.status})`);
  return payload;
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
  saveTokens({
    access_token: tokenResponse.access_token,
    expires_in: tokenResponse.expires_in,
    token_type: tokenResponse.token_type,
  });
  sessionStorage.removeItem("vonavy_pkce_verifier");
  sessionStorage.removeItem("vonavy_oauth_state");
  history.replaceState({}, "", "/");
}

function logout() {
  sessionStorage.removeItem("vonavy_tokens");
  state.tokens = null;
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
  if (run.status === "succeeded" && result) {
    const wape = result.holdout?.wape;
    const quality = typeof wape === "number" ? ` Holdout WAPE ${(wape * 100).toFixed(2)}%.` : "";
    return `Forecast complete: ${result.profile.entities * 7} rows.${quality}`;
  }
  if (run.status === "invalid" && result) {
    return result.failure?.message || "The forecast mapping or data is invalid.";
  }
  if (run.status === "failed") return run.failure?.message || "Forecast worker failed.";
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
function showForecastResult(output, run, result) {
  output.replaceChildren(document.createTextNode(forecastMessage(run, result)));
  for (const [name, url] of Object.entries(result.downloads || {})) {
    const link = document.createElement("a");
    link.href = url;
    link.textContent = `Download ${name}`;
    link.rel = "noopener noreferrer";
    link.className = "artifact-link";
    output.append(document.createTextNode(" "), link);
  }
}
async function waitForForecast(run, output, button) {
  const terminal = new Set(["succeeded", "invalid", "failed"]);
  let current = run;
  const maxAttempts = Math.ceil((state.config.forecastJobTimeoutSeconds + 900) / 3);
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    output.textContent = forecastMessage(current);
    if (terminal.has(current.status)) {
      if (current.resultAvailable) {
        const result = await api(current.links.result);
        showForecastResult(output, current, result);
      }
      button.disabled = false;
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
    current = await api(current.links.status);
  }
  output.textContent = "Forecast is still running. Refresh to check it again.";
  button.disabled = false;
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
      "Choose model: xgboost or neuralnet",
      "neuralnet",
    );
    if (modelChoice === null) throw new Error("Forecast setup cancelled.");
    const normalisedModel = modelChoice.trim().toLowerCase();
    const adapterId = normalisedModel === "neuralnet"
      ? "neuralnet-direct-v1"
      : normalisedModel === "xgboost"
        ? "xgboost-direct-v1"
        : null;
    if (!adapterId) throw new Error("Model must be xgboost or neuralnet.");
    const modelLabel = adapterId === "neuralnet-direct-v1"
      ? "Best NeuralNet"
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
        `${warnings}\n\nStart ${modelLabel} on the CPU scale-to-zero worker?`,
    );
    if (!approved) throw new Error("Forecast plan was not confirmed.");
    output.textContent = `Submitting the confirmed ${modelLabel} retraining plan…`;
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
        forecastDataset(dataset, validationStatus, forecastButton);
      });
      actions.append(forecastButton, validationStatus);
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
  $("signed-out").classList.toggle("hidden", Boolean(state.tokens));
  $("signed-in").classList.toggle("hidden", !state.tokens);
  if (state.tokens) {
    const claims = decodeJwt(state.tokens.access_token);
    $("identity").textContent = claims.username || claims.sub;
    await listDatasets();
  }
}

$("login").addEventListener("click", login);
$("logout").addEventListener("click", logout);
$("upload-form").addEventListener("submit", upload);
$("refresh").addEventListener("click", () => {
  listDatasets().catch((error) => {
    $("status").textContent = error.message;
  });
});
start().catch((error) => {
  $("signed-out").classList.remove("hidden");
  $("signed-out").querySelector("p").textContent = error.message;
});
