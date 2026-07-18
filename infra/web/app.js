const state = { config: null, tokens: null };
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
    $("status").textContent = "Upload complete. Validation execution arrives in Phase 2.";
    $("upload-form").reset();
    await listDatasets();
  } catch (error) {
    $("status").textContent = error.message;
  } finally {
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
    item.append(title, meta);
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
