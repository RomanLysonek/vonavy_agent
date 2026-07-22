const API_REQUEST_TIMEOUT_MS = 15_000;
const API_RETRY_DELAYS_MS = [250, 750];
const RETRYABLE_API_STATUSES = new Set([429, 502, 503, 504]);

const state = {
  config: null,
  tokens: null,
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

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.tokens) headers.set("authorization", `Bearer ${state.tokens.access_token}`);
  if (options.body && !headers.has("content-type")) headers.set("content-type", "application/json");

  const method = String(options.method || "GET").toUpperCase();
  const attempts = method === "GET" ? 3 : 1;
  let lastError = null;

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), API_REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`${state.config.apiBaseUrl}${path}`, {
        ...options,
        method,
        headers,
        signal: controller.signal,
      });
      const requestId = response.headers.get("x-vonavy-request-id");
      const sourceRevision = response.headers.get("x-vonavy-source-revision");
      if (sourceRevision) state.sourceRevision = sourceRevision;
      const payload = await response.json().catch(() => ({}));
      if (response.ok) return payload;

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
        clearTimeout(timeout);
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
        clearTimeout(timeout);
        await sleep(API_RETRY_DELAYS_MS[attempt]);
        continue;
      }
      throw wrapped;
    } finally {
      clearTimeout(timeout);
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

function showForecastResult(output, run, result) {
  output.replaceChildren(document.createTextNode(forecastMessage(run, result)));
  appendResultReview(output, result);
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

function setAgentPlan(plan) {
  if (!agentContext) return;
  agentContext.plan = plan || null;
  const confirm = $("agent-confirm");
  if (!plan) {
    $("agent-plan").classList.add("hidden");
    $("agent-plan-summary").replaceChildren();
    confirm.disabled = true;
    return;
  }
  renderAgentPlan(plan);
  confirm.disabled = false;
}

function renderAgentPlan(plan) {
  const root = $("agent-plan-summary");
  root.replaceChildren();
  const preprocessing = plan.preprocessingPlan;
  const lines = [
    plan.summary,
    `Model: ${plan.adapter.label}`,
    `Target: ${plan.mapping.targetColumn}`,
    `Timestamp: ${plan.mapping.timestampColumn}`,
    `Entity: ${plan.mapping.entityColumn || "single series"}`,
    `Training end: ${plan.trainingEnd}`,
    `Forecast: ${plan.forecastStart} through ${plan.forecastEnd}`,
  ];
  if (plan.warnings.length) lines.push(`Warnings: ${plan.warnings.join("; ")}`);
  for (const line of lines) {
    const paragraph = document.createElement("p");
    paragraph.textContent = line;
    root.append(paragraph);
  }
  if (preprocessing) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const operations = Array.isArray(preprocessing.operations) ? preprocessing.operations : [];
    summary.textContent = `Preprocessing: ${operations.length} fixed operations`;
    details.append(summary);
    const metadata = document.createElement("p");
    const digest = preprocessing.digest?.value || "unavailable";
    metadata.textContent = `Catalogue: ${preprocessing.catalogVersion} · plan digest: ${digest.slice(0, 12)}…`;
    details.append(metadata);
    const review = preprocessing.review || {};
    const findings = Array.isArray(preprocessing.findings) ? preprocessing.findings : [];
    const reviewLine = document.createElement("p");
    reviewLine.textContent = `Preprocessing review: ${review.status || "unavailable"} · max severity: ${review.maxSeverity || "unavailable"} · ${findings.length} findings`;
    details.append(reviewLine);
    const attentionFindings = findings.filter((finding) => finding.severity === "warning");
    if (attentionFindings.length) {
      const findingsList = document.createElement("ul");
      for (const finding of attentionFindings) {
        const item = document.createElement("li");
        item.textContent = `${finding.severity}: ${finding.message}`;
        findingsList.append(item);
      }
      details.append(findingsList);
    }
    const list = document.createElement("ol");
    for (const operation of operations) {
      const item = document.createElement("li");
      item.textContent = `${operation.action} (${operation.status})`;
      list.append(item);
    }
    details.append(list);
    root.append(details);
  }
  $("agent-plan").classList.remove("hidden");
}

async function agenticForecastDataset(dataset, output, button) {
  const validation = state.validationResults.get(dataset.datasetId);
  if (!validation?.jobId) {
    output.textContent = "Validate this dataset first so the agent can inspect its safe profile.";
    return;
  }
  button.disabled = true;
  agentContext = {
    dataset,
    output,
    button,
    validation,
    session: null,
    plan: null,
    running: false,
    pending: false,
  };
  $("agent-title").textContent = `Plan a forecast for ${dataset.name}`;
  $("agent-messages").replaceChildren();
  $("agent-status").textContent = "";
  $("agent-input").value = "";
  $("agent-input").disabled = false;
  $("agent-send").disabled = false;
  setAgentPlan(null);
  appendAgentMessage(
    "assistant",
    "Tell me the forecasting objective. I can inspect the validated metadata, compare XGBoost, the Direct NeuralNet, and Chronos-2, compile a fixed safe preprocessing plan, then prepare everything for your confirmation.",
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
  setAgentPlan(null);
  $("agent-status").textContent = "Queueing the agent turn…";
  try {
    const requestToken = crypto.randomUUID();
    const queued = agentContext.session
      ? await api(agentContext.session.links.messages, {
        method: "POST",
        body: JSON.stringify({ message, requestToken }),
      })
      : await api(`/api/datasets/${agentContext.dataset.datasetId}/forecast-agent/sessions`, {
        method: "POST",
        body: JSON.stringify({
          validationJobId: agentContext.validation.jobId,
          message,
          requestToken,
        }),
      });
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
    if (session.draftPlan) setAgentPlan(session.draftPlan);
    $("agent-status").textContent = session.draftPlan
      ? "Review the plan or continue chatting to revise it."
      : `Turn ${session.turnCount} of 8`;
  } catch (error) {
    appendAgentMessage("assistant", `I could not complete that turn: ${error.message}`);
    $("agent-status").textContent = "Agent turn failed. You can try again.";
  } finally {
    if (agentContext) {
      agentContext.pending = false;
      input.disabled = false;
      $("agent-send").disabled = false;
      input.focus();
    }
  }
}

async function confirmAgentPlan() {
  if (!agentContext) return;
  if (agentContext.pending) {
    $("agent-status").textContent = "Wait for the current agent turn to finish.";
    return;
  }
  if (agentContext.running) {
    $("agent-status").textContent = "The confirmed forecast is already being submitted.";
    return;
  }
  if (!agentContext.plan) {
    $("agent-status").textContent = "Ask the agent to prepare a confirmable plan first.";
    appendAgentMessage("assistant", "A confirmable plan has not been created yet.");
    return;
  }
  const plan = agentContext.plan;
  const preprocessing = plan.preprocessingPlan;
  const operationCount = Array.isArray(preprocessing?.operations) ? preprocessing.operations.length : 0;
  const preprocessingDigest = preprocessing?.digest?.value || "unavailable";
  const preprocessingReview = preprocessing?.review?.status || "unavailable";
  const attentionCount = Array.isArray(preprocessing?.review?.attentionFindingIds)
    ? preprocessing.review.attentionFindingIds.length
    : 0;
  const approved = window.confirm(
    `${plan.summary}\n\nModel: ${plan.adapter.label}\n` +
      `Target: ${plan.mapping.targetColumn}\nTraining end: ${plan.trainingEnd}\n` +
      `Forecast: ${plan.forecastStart} through ${plan.forecastEnd}\n` +
      `Preprocessing: ${operationCount} fixed operations\n` +
      `Review: ${preprocessingReview} (${attentionCount} attention findings)\n` +
      `Plan digest: ${preprocessingDigest.slice(0, 12)}…\n\n` +
      "Only validated metadata was sent to Bedrock. Confirm this immutable plan and start the scale-to-zero workflow?",
  );
  if (!approved) return;
  agentContext.running = true;
  $("agent-confirm").disabled = true;
  $("agent-status").textContent = `Submitting ${plan.adapter.label}…`;
  try {
    const run = await api(`/api/datasets/${agentContext.dataset.datasetId}/forecasts`, {
      method: "POST",
      body: JSON.stringify({
        requestToken: crypto.randomUUID(),
        adapterId: plan.adapterId,
        trainingEnd: plan.trainingEnd,
        mapping: plan.mapping,
      }),
    });
    const { output, button } = agentContext;
    $("agent-dialog").close();
    output.textContent = `Submitted ${plan.adapter.label} after explicit confirmation.`;
    await waitForForecast(run, output, button);
    agentContext = null;
  } catch (error) {
    appendAgentMessage("assistant", `The confirmed workflow could not start: ${error.message}`);
    $("agent-status").textContent = "Submission failed.";
    $("agent-confirm").disabled = false;
    agentContext.running = false;
  }
}

function closeAgentDialog() {
  if (agentContext && !agentContext.running) agentContext.button.disabled = false;
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
$("agent-form").addEventListener("submit", sendAgentMessage);
$("agent-confirm").addEventListener("click", confirmAgentPlan);
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
