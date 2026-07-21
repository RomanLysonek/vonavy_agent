import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const start = source.indexOf("const API_REQUEST_TIMEOUT_MS");
const end = source.indexOf("async function login()", start);
if (start < 0 || end < 0) throw new Error("API client source anchors are missing");

const context = vm.createContext({
  Headers,
  Response,
  AbortController,
  setTimeout,
  clearTimeout,
  console,
  fetch: null,
});
vm.runInContext(source.slice(start, end), context);
vm.runInContext('state.config = { apiBaseUrl: "https://example.invalid" };', context);

let getCalls = 0;
context.fetch = async () => {
  getCalls += 1;
  if (getCalls < 3) {
    return new Response(JSON.stringify({ error: { message: "temporary" } }), {
      status: 503,
      headers: {
        "content-type": "application/json",
        "x-vonavy-request-id": `00000000-0000-4000-8000-00000000000${getCalls}`,
        "x-vonavy-source-revision": "0123456789abcdef0123456789abcdef01234567",
      },
    });
  }
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: {
      "content-type": "application/json",
      "x-vonavy-request-id": "00000000-0000-4000-8000-000000000003",
      "x-vonavy-source-revision": "0123456789abcdef0123456789abcdef01234567",
    },
  });
};
const getResult = await vm.runInContext('api("/api/health")', context);
if (!getResult.ok || getCalls !== 3) throw new Error(`GET attempts=${getCalls}`);

let postCalls = 0;
context.fetch = async () => {
  postCalls += 1;
  return new Response(JSON.stringify({ error: { message: "temporary mutation failure" } }), {
    status: 503,
    headers: {
      "content-type": "application/json",
      "x-vonavy-request-id": "00000000-0000-4000-8000-000000000004",
      "x-vonavy-source-revision": "0123456789abcdef0123456789abcdef01234567",
    },
  });
};
try {
  await vm.runInContext(
    'api("/api/upload-sessions", { method: "POST", body: "{}" })',
    context,
  );
  throw new Error("POST unexpectedly succeeded");
} catch (error) {
  if (postCalls !== 1) throw new Error(`POST attempts=${postCalls}`);
  if (!String(error.message).includes("reference 00000000-0000-4000-8000-000000000004")) {
    throw error;
  }
  if (!String(error.message).includes("deployment 0123456789ab")) throw error;
}

console.log(JSON.stringify({ getCalls, postCalls, status: "passed" }));
