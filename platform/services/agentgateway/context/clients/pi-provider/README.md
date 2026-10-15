# pi provider for the Uhstray inference gateway

A [pi](https://pi.dev) package that adds the `uhstray` provider: sign in with your gateway key
using pi's own `/login`, and the model list comes live from the gateway. Streaming is pi's built-in
OpenAI Chat Completions client, so tool calls, usage accounting and cancellation behave like any
other pi provider.

The package holds no secret. The key you type is validated against the gateway and then stored by
pi in `auth.json` (written `0600`), never in this directory.

| What | How |
|---|---|
| Gateway | `https://inference.uhstray.io/v1` (OpenAI-compatible, `Authorization: Bearer <key>`) |
| Sign-in | `/login uhstray`, paste the key (masked) |
| Key check | `GET /v1/models`; a 401 refuses the login and stores nothing |
| Models | The ids the gateway lists for your key. Context windows come from a table in `gateway.ts`; an id not in it gets 131072 tokens of context, 16384 output, reasoning on, text input, zero cost. Ids are kept only if they are 1-128 characters of `A-Z a-z 0-9 . _ : / @ + -`, and at most 500 are used; any other id is dropped without a message |
| Streaming | `openai-completions`, no custom code |

## Install

From a checkout of this repository, at its root:

```bash
pi install "$(pwd)/platform/services/agentgateway/context/clients/pi-provider"
```

A local path is loaded in place, not copied, so `git pull` updates it. Reload a running session
with `/reload`. Requires pi with native provider support (the version this was built against is
1.1.0) and no `npm install`: pi supplies `@earendil-works/pi-ai` itself.

## Sign in

1. Start pi and run `/login uhstray` (plain `/login` opens the provider picker instead).
2. Paste your gateway key when asked (masked).
3. Pi shows "Saved API key" and refreshes the model list. No default model is configured for this
   provider, so choose one with `/model`, or start with `--provider uhstray --model <id>`.

Keys are issued per client by the platform operators; see "Add a client" in
[`../../architecture.md`](../../architecture.md).

Instead of `/login`, you can export `UHSTRAY_INFERENCE_API_KEY`. A key stored by `/login` wins over
the variable. Prefer `/login`: an exported variable is visible to every process you start.

To use another gateway, for example the local-dev one, set `UHSTRAY_INFERENCE_BASE_URL`
(`https://inference.agent-cloud.test:8443/v1`). It must be https; plain http is accepted only for
`localhost`, `127.0.0.1` and `[::1]`, because the key travels on it. A URL carrying credentials,
a query or a fragment is refused. Whatever that variable points at receives the stored key as
well, so only processes you trust should be able to set it.

## Verify

```bash
pi --list-models uhstray
pi -p --provider uhstray --model qwen3.8-flash-next "Reply with the single word: ok"
```

Pi keeps the model list from the last successful refresh in `models-store.json` beside
`auth.json` (model ids and limits only, no key). It refreshes from the network when an interactive
or RPC session starts and right after `/login`, not for `--list-models` or `-p`. So on a fresh
install, run `/login` (or open pi once, with the key already stored or exported) before the two
commands above show models. If the gateway is unreachable at start, the last list stays in use.

`pi auth check --provider uhstray` reports `provider_not_found`: that command builds its own
runtime without loading extensions, so it cannot see extension providers. Use the two commands
above instead.

## Uninstall

```bash
pi remove "$(pwd)/platform/services/agentgateway/context/clients/pi-provider"
```

Then `/logout` and pick the provider to delete the stored key.

## Develop

```bash
cd platform/services/agentgateway/context/clients/pi-provider
node --test gateway.test.ts      # Node 22.18+
```

`gateway.ts` holds every decision (URL rules, key check, model mapping, auth resolution) and
imports nothing from pi at runtime, so the tests need no pi install. `index.ts` is the wiring:
`createProvider` from `@earendil-works/pi-ai` with `openAICompletionsApi()`, registered through
`pi.registerProvider`. Both come from the `compat` entry because that is what pi aliases for
extensions.

Nothing in this package logs, and no error carries the key, a header or a response body.
