import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const start = source.indexOf("const API_REQUEST_TIMEOUT_MS");
const end = source.indexOf("async function login()", start);
if (start < 0 || end < 0) throw new Error("Authentication client source anchors are missing");

const stored = new Map();
const sessionStorage = {
  getItem: (key) => stored.get(key) ?? null,
  setItem: (key, value) => stored.set(key, String(value)),
  removeItem: (key) => stored.delete(key),
};
const classList = { toggle() {}, add() {}, remove() {} };
const document = { getElementById: () => ({ classList }) };
const context = vm.createContext({
  AbortController,
  Headers,
  Response,
  URLSearchParams,
  atob,
  btoa,
  clearTimeout,
  console,
  document,
  fetch: null,
  localStorage: sessionStorage,
  sessionStorage,
  setTimeout,
});
vm.runInContext(source.slice(start, end), context);
vm.runInContext(
  'state.config = { apiBaseUrl: "https://api.example.invalid", cognitoDomain: "https://auth.example.invalid", userPoolClientId: "client" };',
  context,
);

function token(expiresAt) {
  const payload = Buffer.from(
    JSON.stringify({ exp: expiresAt, sub: "owner", username: "roman" }),
  ).toString("base64url");
  return `header.${payload}.signature`;
}
function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const now = Math.floor(Date.now() / 1000);
const expired = token(now - 60);
const fresh = token(now + 3600);
let tokenCalls = 0;
let apiCalls = 0;
vm.runInContext(
  `saveTokens({ access_token: ${JSON.stringify(expired)}, refresh_token: "refresh-one" })`,
  context,
);
context.fetch = async (url, options) => {
  if (String(url).endsWith("/oauth2/token")) {
    tokenCalls += 1;
    const body = new URLSearchParams(options.body);
    assert.equal(body.get("grant_type"), "refresh_token");
    assert.equal(body.get("client_id"), "client");
    assert.equal(body.get("refresh_token"), "refresh-one");
    return jsonResponse({ access_token: fresh, expires_in: 3600, token_type: "Bearer" });
  }
  apiCalls += 1;
  assert.equal(options.headers.get("authorization"), `Bearer ${fresh}`);
  return jsonResponse({ created: true }, 201);
};
const preflight = await vm.runInContext(
  'api("/api/forecasts/run/agent/sessions", { method: "POST", body: "{}" })',
  context,
);
assert.equal(preflight.created, true);
assert.equal(tokenCalls, 1);
assert.equal(apiCalls, 1);
assert.equal(vm.runInContext("state.tokens.refresh_token", context), "refresh-one");

const initiallyAccepted = token(now + 3000);
const refreshedAfter401 = token(now + 4000);
tokenCalls = 0;
apiCalls = 0;
vm.runInContext(
  `saveTokens({ access_token: ${JSON.stringify(initiallyAccepted)}, refresh_token: "refresh-two" })`,
  context,
);
context.fetch = async (url, options) => {
  if (String(url).endsWith("/oauth2/token")) {
    tokenCalls += 1;
    return jsonResponse({ access_token: refreshedAfter401, expires_in: 3600 });
  }
  apiCalls += 1;
  if (apiCalls === 1) return jsonResponse({}, 401);
  assert.equal(options.headers.get("authorization"), `Bearer ${refreshedAfter401}`);
  return jsonResponse({ queued: true }, 201);
};
const replayed = await vm.runInContext(
  'api("/api/forecasts/run/agent/sessions", { method: "POST", body: "{}" })',
  context,
);
assert.equal(replayed.queued, true);
assert.equal(tokenCalls, 1);
assert.equal(apiCalls, 2);
assert.equal(vm.runInContext("state.tokens.refresh_token", context), "refresh-two");

vm.runInContext("clearTokens()", context);
vm.runInContext(`saveTokens({ access_token: ${JSON.stringify(expired)} })`, context);
assert.equal(vm.runInContext("loadTokens()", context), null);
assert.equal(sessionStorage.getItem("vonavy_tokens"), null);

console.log(JSON.stringify({ apiCalls, status: "passed", tokenCalls }));
