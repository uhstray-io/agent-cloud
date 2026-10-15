/**
 * Uhstray inference gateway provider for pi.
 *
 * - `/login uhstray` asks for a gateway key, proves it against `GET /v1/models`, and pi stores it
 *   in auth.json. `UHSTRAY_INFERENCE_API_KEY` works as a fallback when nothing is stored.
 * - The model list is the gateway's own, filtered per key, refreshed by pi and kept as pi's
 *   persisted catalog snapshot for offline starts.
 * - Streaming is pi's built-in OpenAI Chat Completions implementation; nothing custom.
 *
 * Secret-free: the key is never in this package, and nothing here logs it.
 */
import { createProvider, openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  BASE_URL_ENV,
  DEFAULT_BASE_URL,
  discoverModels,
  GatewayError,
  loginWithKey,
  normalizeBaseUrl,
  PROVIDER_ID,
  resolveAuth,
} from "./gateway.ts";

export default function (pi: ExtensionAPI) {
  pi.registerProvider(
    createProvider({
      id: PROVIDER_ID,
      name: "Uhstray inference gateway",
      baseUrl: DEFAULT_BASE_URL,
      auth: {
        apiKey: {
          name: "Uhstray gateway key",
          login: (interaction) => loginWithKey(interaction, normalizeBaseUrl(process.env[BASE_URL_ENV])),
          resolve: resolveAuth,
        },
      },
      // Purely dynamic: a baseline would keep listing a model the key has lost access to.
      models: [],
      fetchModels: async (context) => {
        const key = context.credential?.type === "api_key" ? context.credential.key : undefined;
        if (!key) throw new GatewayError("no gateway key; run /login uhstray");
        return discoverModels(normalizeBaseUrl(process.env[BASE_URL_ENV]), key, context.signal);
      },
      api: openAICompletionsApi(),
    }),
  );
}
