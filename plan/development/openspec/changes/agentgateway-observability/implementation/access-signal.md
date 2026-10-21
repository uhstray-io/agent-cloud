# OTLP access signal discriminator

## Scope

The gateway's OTLP access-log field list now adds the literal `signal: "access-log"` alongside the existing service, identity, and query-free path fields. The discriminator is scoped to `accessLog.otlp.fields.add`; stdout access-log fields and tracing attributes remain unchanged. No trace or log switches, model fields, metric labels, content capture, identities, or sampling behavior are changed by this slice.

The gateway image is pinned to `cr.agentgateway.dev/agentgateway:v1.5.0` in `platform/services/agentgateway/deployment/compose.yml`. The matching upstream v1.5.0 config schema defines the OTLP access-log `fields` customization with an `add` map of CEL expressions: [agentgateway v1.5.0 schema/config.md, access log OTLP fields](https://github.com/agentgateway/agentgateway/blob/v1.5.0/schema/config.md#L18218-L18230). This supports adding the static discriminator at the existing `fields.add` level.

## Render check

`platform/tests/test_agentgateway_access_signal.py` renders the real template and checks that the OTLP record carries the discriminator and existing bounded fields, while stdout and trace fields do not. It also asserts the path remains `request.path` and that no URI field is introduced.
