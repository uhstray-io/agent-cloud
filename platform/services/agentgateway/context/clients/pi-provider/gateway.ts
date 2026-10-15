/**
 * Gateway logic for the pi provider: base URL, key check, model discovery, auth resolution.
 *
 * Kept free of runtime imports from pi (types only, which Node erases) so `gateway.test.ts` runs
 * with plain `node --test` — a locally installed pi package gets no node_modules of its own.
 *
 * Nothing here logs, and no error message carries the key, a header or a response body.
 */
import type { ApiKeyCredential, AuthResult, Model } from "@earendil-works/pi-ai";

export const PROVIDER_ID = "uhstray";
export const DEFAULT_BASE_URL = "https://inference.uhstray.io/v1";
/** Environment fallback for the key; a key stored by `/login` wins over it. */
export const KEY_ENV = "UHSTRAY_INFERENCE_API_KEY";
/** Points the provider at another gateway, e.g. the local-dev one. */
export const BASE_URL_ENV = "UHSTRAY_INFERENCE_BASE_URL";

export type GatewayModel = Model<"openai-completions">;

/** What `/v1/models` does not tell us, per model id. Unknown ids get the conservative defaults. */
// A Map, not an object literal: ids come from the network, and `constructor` or `__proto__` must
// not resolve to something off Object.prototype.
const CONTEXT_WINDOWS: ReadonlyMap<string, number> = new Map([
  ["qwen3.8-flash-next", 262144],
  ["xiaomi/mimo-v2.6-flash", 1050000],
  ["deepseek/deepseek-v4.1-flash", 1048576],
  ["z-ai/glm-5.3-flash", 1048576],
  ["xiaomi/mimo-v2.6-pro", 1050000],
  ["openai/gpt-6-luna", 1050000],
  ["mistralai/mistral-large-4-0", 1048576],
  ["z-ai/glm-5.3", 1048576],
  ["anthropic/claude-opus-5.5", 1000000],
]);
const DEFAULT_CONTEXT_WINDOW = 131072;
const MAX_TOKENS = 16384;
/** Ids reach a terminal and a request body: keep a conservative alphabet and bound the list. */
const MODEL_ID_PATTERN = /^[A-Za-z0-9._:/@+_-]{1,128}$/;
const MAX_MODELS = 500;

/** A failure whose message is safe to show: it never contains the key or any response content. */
export class GatewayError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GatewayError";
  }
}

export interface AuthContextLike {
  env(name: string): Promise<string | undefined>;
}

type FetchLike = typeof fetch;

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/**
 * Normalized, trailing-slash-free `/v1` base. Cleartext http is allowed only on loopback: the key
 * rides on it. Credentials, query and fragment in the URL are refused: they would be sent or
 * dropped silently.
 */
export function normalizeBaseUrl(raw?: string): string {
  const value = raw?.trim();
  if (!value) return DEFAULT_BASE_URL;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new GatewayError(`${BASE_URL_ENV} is not a valid URL`);
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname))) {
    throw new GatewayError(`${BASE_URL_ENV} must be https (http only for localhost)`);
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new GatewayError(`${BASE_URL_ENV} must not contain credentials, a query or a fragment`);
  }
  return url.origin + url.pathname.replace(/\/+$/, "");
}

/** A key the gateway could have issued: printable ASCII, no whitespace. Stops header injection. */
function assertWellFormedKey(key: string): void {
  if (!/^[\x21-\x7e]+$/.test(key)) {
    throw new GatewayError("the gateway key is empty or contains characters not allowed in a header");
  }
}

function modelFor(id: string, baseUrl: string): GatewayModel {
  return {
    id,
    name: id,
    api: "openai-completions",
    provider: PROVIDER_ID,
    baseUrl,
    reasoning: true,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: CONTEXT_WINDOWS.get(id) ?? DEFAULT_CONTEXT_WINDOW,
    maxTokens: MAX_TOKENS,
  };
}

/** Map a `GET /v1/models` payload (`{data: [{id}]}`, already filtered per key) to pi models. */
export function parseModelList(payload: unknown, baseUrl: string): GatewayModel[] {
  const data = (payload as { data?: unknown } | null)?.data;
  if (!Array.isArray(data)) throw new GatewayError("the gateway's model list has an unexpected shape");
  const seen = new Set<string>();
  const models: GatewayModel[] = [];
  for (const entry of data) {
    const id = (entry as { id?: unknown } | null)?.id;
    if (typeof id !== "string" || !MODEL_ID_PATTERN.test(id) || seen.has(id)) continue;
    seen.add(id);
    models.push(modelFor(id, baseUrl));
    if (models.length === MAX_MODELS) break;
  }
  return models;
}

/**
 * `GET {baseUrl}/models` with the key. Resolves the parsed JSON body.
 * 401/403 and transport failures become fixed messages; an abort propagates untouched.
 */
export async function fetchModelsPayload(
  baseUrl: string,
  key: string,
  signal?: AbortSignal,
  fetchImpl: FetchLike = fetch,
): Promise<unknown> {
  assertWellFormedKey(key);
  let response: Response;
  try {
    response = await fetchImpl(`${baseUrl}/models`, {
      method: "GET",
      headers: { Authorization: `Bearer ${key}`, Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new GatewayError("could not reach the gateway");
  }
  if (response.status === 401 || response.status === 403) {
    throw new GatewayError(`the gateway rejected the key (HTTP ${response.status})`);
  }
  if (!response.ok) throw new GatewayError(`the gateway answered HTTP ${response.status}`);
  try {
    return await response.json();
  } catch {
    if (signal?.aborted) throw signal.reason;
    throw new GatewayError("the gateway's model list is not JSON");
  }
}

export async function discoverModels(
  baseUrl: string,
  key: string,
  signal?: AbortSignal,
  fetchImpl: FetchLike = fetch,
): Promise<GatewayModel[]> {
  return parseModelList(await fetchModelsPayload(baseUrl, key, signal, fetchImpl), baseUrl);
}

export interface LoginInteractionLike {
  signal: AbortSignal;
  prompt(prompt: { type: "secret"; message: string }): Promise<string>;
}

/** `/login uhstray`: ask for the key, prove it against the gateway, hand it to pi to store. */
export async function loginWithKey(
  interaction: LoginInteractionLike,
  baseUrl: string,
  fetchImpl: FetchLike = fetch,
): Promise<ApiKeyCredential> {
  interaction.signal.throwIfAborted();
  const key = (await interaction.prompt({ type: "secret", message: "Enter the Uhstray gateway key" })).trim();
  interaction.signal.throwIfAborted();
  await fetchModelsPayload(baseUrl, key, interaction.signal, fetchImpl);
  return { type: "api_key", key };
}

/**
 * Stored key first, then the environment. The base URL is always returned, so a persisted
 * model list from another gateway never decides where a request goes.
 */
export async function resolveAuth(input: {
  ctx: AuthContextLike;
  credential?: { key?: string };
}): Promise<AuthResult | undefined> {
  const stored = input.credential?.key;
  const key = stored || (await input.ctx.env(KEY_ENV));
  if (!key) return undefined;
  const baseUrl = normalizeBaseUrl(await input.ctx.env(BASE_URL_ENV));
  return { auth: { apiKey: key, baseUrl }, source: stored ? "stored credential" : KEY_ENV };
}
