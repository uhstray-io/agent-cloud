/**
 * Run: `node --test gateway.test.ts` (Node 22.18+, which strips types by default).
 *
 * The provider wiring in index.ts needs pi's own packages, so it is proven live instead (README,
 * "Verify"). Everything that decides what is sent, stored or shown lives in gateway.ts and is
 * tested here with an injected fetch.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BASE_URL_ENV,
  DEFAULT_BASE_URL,
  discoverModels,
  fetchModelsPayload,
  GatewayError,
  KEY_ENV,
  loginWithKey,
  normalizeBaseUrl,
  parseModelList,
  PROVIDER_ID,
  resolveAuth,
} from "./gateway.ts";

// Not a real key; long and distinctive so a leak is unmistakable.
const KEY = "agw-test-KEY-0123456789abcdef";
const BASE = "https://gateway.example.invalid/v1";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const modelsBody = (...ids: string[]) => ({ object: "list", data: ids.map((id) => ({ id, object: "model" })) });

const KNOWN: Record<string, number> = {
  "qwen3.8-flash-next": 262144,
  "xiaomi/mimo-v2.6-flash": 1050000,
  "deepseek/deepseek-v4.1-flash": 1048576,
  "z-ai/glm-5.3-flash": 1048576,
  "xiaomi/mimo-v2.6-pro": 1050000,
  "openai/gpt-6-luna": 1050000,
  "mistralai/mistral-large-4-0": 1048576,
  "z-ai/glm-5.3": 1048576,
};

test("every known id maps to its context window and the shared metadata", () => {
  const models = parseModelList(modelsBody(...Object.keys(KNOWN)), BASE);
  assert.equal(models.length, 8);
  for (const model of models) {
    assert.equal(model.contextWindow, KNOWN[model.id], model.id);
    assert.equal(model.maxTokens, 16384);
    assert.equal(model.api, "openai-completions");
    assert.equal(model.provider, PROVIDER_ID);
    assert.equal(model.baseUrl, BASE);
    assert.equal(model.reasoning, true);
    assert.deepEqual(model.input, ["text"]);
    assert.deepEqual(model.cost, { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
  }
});

test("an unknown id gets the conservative defaults", () => {
  const [model] = parseModelList(modelsBody("some/new-model"), BASE);
  assert.equal(model.id, "some/new-model");
  assert.equal(model.contextWindow, 131072);
  assert.equal(model.maxTokens, 16384);
  assert.equal(model.reasoning, true);
  assert.deepEqual(model.input, ["text"]);
});

test("duplicates and entries without a usable id are dropped, order kept", () => {
  const payload = { data: [{ id: "b" }, { id: "a" }, { id: "b" }, { id: "" }, { id: 7 }, {}, null] };
  assert.deepEqual(
    parseModelList(payload, BASE).map((model) => model.id),
    ["b", "a"],
  );
});

test("an empty list is a valid answer (a key may be allowed no model)", () => {
  assert.deepEqual(parseModelList({ data: [] }, BASE), []);
});

test("a payload without a data array is refused, naming no content", () => {
  for (const bad of [null, "x", 3, {}, { data: "nope" }, { data: { id: "a" } }]) {
    assert.throws(() => parseModelList(bad, BASE), GatewayError);
  }
});

test("ids that name Object.prototype members get the default window, not a function", () => {
  const ids = ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf"];
  const models = parseModelList(modelsBody(...ids), BASE);
  assert.deepEqual(
    models.map((model) => model.id),
    ids,
  );
  for (const model of models) assert.equal(model.contextWindow, 131072, model.id);
});

test("ids outside the safe alphabet or length are dropped; the list is capped", () => {
  const bad = ["x".repeat(100_000), "x".repeat(129), "ok\u001b[31mred", "has space", "new\nline", "ünï", "a;b"];
  const good = ["x".repeat(128), "a/b:c@d+e-f_g.h"];
  assert.deepEqual(
    parseModelList(modelsBody(...bad, ...good), BASE).map((model) => model.id),
    good,
  );
  const many = Array.from({ length: 800 }, (_, i) => `m${i}`);
  assert.equal(parseModelList(modelsBody(...many), BASE).length, 500);
});

test("loopback http is exact: lookalike hosts, userinfo tricks and a trailing dot are refused", () => {
  for (const bad of [
    "http://127.0.0.1.evil.com/v1",
    "http://localhost.evil/v1",
    "http://localhost@evil.com/v1",
    "http://127.0.0.1@evil.com/v1",
    "http://localhost./v1",
    "http://[::2]:4000/v1",
  ]) {
    assert.throws(() => normalizeBaseUrl(bad), GatewayError, bad);
  }
  assert.equal(normalizeBaseUrl("http://[::1]:4000/v1"), "http://[::1]:4000/v1");
});

test("a base URL with credentials, a query or a fragment is refused", () => {
  for (const bad of [
    "https://user:pw@gateway.example.invalid/v1",
    "https://user@gateway.example.invalid/v1",
    "https://gateway.example.invalid/v1?x=1",
    "https://gateway.example.invalid/v1#frag",
    "http://localhost:4000/v1?x=1",
  ]) {
    assert.throws(
      () => normalizeBaseUrl(bad),
      (error: Error) => error instanceof GatewayError && !error.message.includes("pw"),
      bad,
    );
  }
});

test("the base URL comes back normalized: no trailing slashes, default port elided", () => {
  assert.equal(normalizeBaseUrl("https://gateway.example.invalid/v1//"), "https://gateway.example.invalid/v1");
  assert.equal(normalizeBaseUrl("https://gateway.example.invalid:443/v1/"), "https://gateway.example.invalid/v1");
  assert.equal(normalizeBaseUrl("https://gateway.example.invalid/"), "https://gateway.example.invalid");
});

test("normalizeBaseUrl: default, trimming, https, loopback http only", () => {
  assert.equal(normalizeBaseUrl(undefined), DEFAULT_BASE_URL);
  assert.equal(normalizeBaseUrl("  "), DEFAULT_BASE_URL);
  assert.equal(normalizeBaseUrl("https://inference.agent-cloud.test:8443/v1/"), "https://inference.agent-cloud.test:8443/v1");
  assert.equal(normalizeBaseUrl("http://localhost:4000/v1"), "http://localhost:4000/v1");
  assert.equal(normalizeBaseUrl("http://127.0.0.1:4000/v1"), "http://127.0.0.1:4000/v1");
  assert.throws(() => normalizeBaseUrl("http://gateway.example.invalid/v1"), GatewayError);
  assert.throws(() => normalizeBaseUrl("ftp://gateway.example.invalid/v1"), GatewayError);
  assert.throws(() => normalizeBaseUrl("not a url"), GatewayError);
});

test("the key request is a GET /models with a Bearer header and the caller's signal", async () => {
  const seen: { url?: string; init?: RequestInit } = {};
  const controller = new AbortController();
  const payload = await fetchModelsPayload(
    BASE,
    KEY,
    controller.signal,
    (async (url: string, init: RequestInit) => {
      Object.assign(seen, { url, init });
      return json(modelsBody("a"));
    }) as unknown as typeof fetch,
  );
  assert.deepEqual(payload, modelsBody("a"));
  assert.equal(seen.url, `${BASE}/models`);
  assert.equal(seen.init?.method, "GET");
  assert.equal((seen.init?.headers as Record<string, string>).Authorization, `Bearer ${KEY}`);
  assert.equal(seen.init?.signal, controller.signal);
});

test("a 401 key is rejected with a fixed message that names no secret", async () => {
  const fetchImpl = (async () => json({ error: { message: `bad key ${KEY}` } }, 401)) as unknown as typeof fetch;
  await assert.rejects(discoverModels(BASE, KEY, undefined, fetchImpl), (error: Error) => {
    assert.ok(error instanceof GatewayError);
    assert.match(error.message, /rejected the key \(HTTP 401\)/);
    assert.ok(!error.message.includes(KEY));
    assert.ok(!error.message.includes("bad key"));
    return true;
  });
});

test("other failures (403, 500, transport, bad JSON) never echo the key or the body", async () => {
  const cases: Record<string, typeof fetch> = {
    forbidden: (async () => json({ detail: KEY }, 403)) as unknown as typeof fetch,
    server: (async () => new Response(`boom ${KEY}`, { status: 500 })) as unknown as typeof fetch,
    transport: (async () => {
      throw new TypeError(`connect failed with Authorization: Bearer ${KEY}`);
    }) as unknown as typeof fetch,
    notJson: (async () => new Response(`<html>${KEY}</html>`, { status: 200 })) as unknown as typeof fetch,
  };
  for (const [name, fetchImpl] of Object.entries(cases)) {
    await assert.rejects(discoverModels(BASE, KEY, undefined, fetchImpl), (error: Error) => {
      assert.ok(error instanceof GatewayError, name);
      assert.ok(!error.message.includes(KEY), name);
      assert.ok(!String(error.stack).includes(KEY), name);
      return true;
    });
  }
});

test("an abort is passed through as the abort, not rewritten as a gateway failure", async () => {
  const controller = new AbortController();
  controller.abort(new Error("cancelled"));
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    init.signal?.throwIfAborted();
    return json(modelsBody("a"));
  }) as unknown as typeof fetch;
  await assert.rejects(discoverModels(BASE, KEY, controller.signal, fetchImpl), /cancelled/);
});

test("a key that is not header-safe is refused before any request, echoing nothing", async () => {
  let called = false;
  const fetchImpl = (async () => {
    called = true;
    return json(modelsBody("a"));
  }) as unknown as typeof fetch;
  for (const bad of ["", "has space", "line\nbreak", `tab\t${KEY}`, "ünïcode"]) {
    await assert.rejects(discoverModels(BASE, bad, undefined, fetchImpl), (error: Error) => {
      assert.ok(error instanceof GatewayError);
      assert.ok(!error.message.includes(bad.trim()) || bad.trim() === "");
      return true;
    });
  }
  assert.equal(called, false);
});

const interactionAnswering = (answer: string) => {
  const prompts: unknown[] = [];
  return {
    prompts,
    interaction: {
      signal: new AbortController().signal,
      prompt: async (prompt: { type: "secret"; message: string }) => {
        prompts.push(prompt);
        return answer;
      },
    },
  };
};

test("login asks for the key as a masked secret, proves it, and returns an api_key credential", async () => {
  const { prompts, interaction } = interactionAnswering(`  ${KEY}\n`);
  let authorization: string | undefined;
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    authorization = (init.headers as Record<string, string>).Authorization;
    return json(modelsBody("a"));
  }) as unknown as typeof fetch;
  const credential = await loginWithKey(interaction, BASE, fetchImpl);
  assert.deepEqual(credential, { type: "api_key", key: KEY });
  assert.equal((prompts[0] as { type: string }).type, "secret");
  assert.equal(authorization, `Bearer ${KEY}`);
});

test("login rejects a 401 key and stores nothing", async () => {
  const { interaction } = interactionAnswering(KEY);
  const fetchImpl = (async () => json({}, 401)) as unknown as typeof fetch;
  await assert.rejects(loginWithKey(interaction, BASE, fetchImpl), (error: Error) => {
    assert.match(error.message, /rejected the key/);
    assert.ok(!error.message.includes(KEY));
    return true;
  });
});

test("login with an empty answer fails before any request", async () => {
  const { interaction } = interactionAnswering("   ");
  let called = false;
  const fetchImpl = (async () => {
    called = true;
    return json({});
  }) as unknown as typeof fetch;
  await assert.rejects(loginWithKey(interaction, BASE, fetchImpl), GatewayError);
  assert.equal(called, false);
});

const ctxWith = (vars: Record<string, string>) => ({ env: async (name: string) => vars[name] });

test("resolveAuth: a stored key wins over the environment", async () => {
  const result = await resolveAuth({
    ctx: ctxWith({ [KEY_ENV]: "from-env" }),
    credential: { key: KEY },
  });
  assert.equal(result?.auth.apiKey, KEY);
  assert.equal(result?.source, "stored credential");
});

test("resolveAuth: the environment is the fallback, and reports which variable", async () => {
  const result = await resolveAuth({ ctx: ctxWith({ [KEY_ENV]: KEY }) });
  assert.equal(result?.auth.apiKey, KEY);
  assert.equal(result?.source, KEY_ENV);
});

test("resolveAuth: no key anywhere means not configured", async () => {
  assert.equal(await resolveAuth({ ctx: ctxWith({}) }), undefined);
  assert.equal(await resolveAuth({ ctx: ctxWith({ [BASE_URL_ENV]: BASE }), credential: {} }), undefined);
});

test("resolveAuth always names the base URL: the default, or the validated override", async () => {
  assert.equal((await resolveAuth({ ctx: ctxWith({}), credential: { key: KEY } }))?.auth.baseUrl, DEFAULT_BASE_URL);
  const local = "https://inference.agent-cloud.test:8443/v1";
  assert.equal(
    (await resolveAuth({ ctx: ctxWith({ [BASE_URL_ENV]: `${local}/` }), credential: { key: KEY } }))?.auth.baseUrl,
    local,
  );
  await assert.rejects(
    resolveAuth({ ctx: ctxWith({ [BASE_URL_ENV]: "http://gateway.example.invalid/v1" }), credential: { key: KEY } }),
    GatewayError,
  );
});
