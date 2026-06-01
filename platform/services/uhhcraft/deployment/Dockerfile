# syntax=docker/dockerfile:1.7
# UhhCraft — Go + templ + Tailwind + sqlc.
# Multi-stage: builder generates code + compiles binary; runtime is distroless.

# ── Stage 1: tool-chains ──────────────────────────────────────────────────────
FROM golang:1.26-alpine AS tools
# libstdc++/libgcc: the Tailwind v4 standalone musl binary is dynamically linked
# against the C++ runtime (cxxabi / _Unwind_* symbols) — bare alpine lacks them.
RUN apk add --no-cache git curl libstdc++ libgcc
# Pin tool versions — bump deliberately, not opportunistically.
ENV TEMPL_VERSION=v0.3.1020 \
    SQLC_VERSION=v1.31.1   \
    GOOSE_VERSION=v3.27.1
RUN go install github.com/a-h/templ/cmd/templ@${TEMPL_VERSION} \
 && go install github.com/sqlc-dev/sqlc/cmd/sqlc@${SQLC_VERSION} \
 && go install github.com/pressly/goose/v3/cmd/goose@${GOOSE_VERSION}

# Tailwind standalone (no Node). v4 — input.css uses v4 syntax (`@import
# "tailwindcss"` + `@theme`), which the old v3 CLI can't parse. Use the -musl
# build since the base image is alpine (musl libc), not glibc.
ARG TAILWIND_VERSION=v4.3.0
RUN curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64-musl" \
      -o /usr/local/bin/tailwindcss \
 && chmod +x /usr/local/bin/tailwindcss

# ── Stage 2: builder ──────────────────────────────────────────────────────────
FROM tools AS builder
WORKDIR /src

# Cache modules.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download

# Source.
COPY . .

# Generate templ code from .templ source.
RUN templ generate

# Generate sqlc typed Go from db/queries + db/migrations.
RUN sqlc generate

# Compile Tailwind to web/static/css/app.css.
RUN tailwindcss \
      -i web/static/css/input.css \
      -o web/static/css/app.css \
      --minify

# Build the server binary.
ARG GIT_SHA=unknown
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build \
      -trimpath \
      -ldflags="-s -w -X main.GitSHA=${GIT_SHA}" \
      -o /out/uhhcraft \
      ./cmd/server

# Strip non-runtime files from web/ to keep the image small.
# Keep: static/ + generated *_templ.go (already compiled into the binary).
RUN mkdir -p /out/web && cp -r web/static /out/web/static

# Also stage the goose binary + migrations so post-deploy.sh can shell in.
RUN cp /go/bin/goose /out/goose
RUN cp -r db /out/db

# ── Stage 3: runtime ──────────────────────────────────────────────────────────
FROM gcr.io/distroless/static-debian12:nonroot AS runtime
WORKDIR /app
COPY --from=builder /out/uhhcraft  /app/uhhcraft
COPY --from=builder /out/web       /app/web
COPY --from=builder /out/goose     /app/goose
COPY --from=builder /out/db        /app/db
COPY config                        /app/config
EXPOSE 3000
USER nonroot:nonroot
# Distroless has no shell — use exec form with the binary's own subcommand,
# which does a cheap local GET /healthz. Mirrors the compose-level healthcheck
# so the image is self-describing when run outside compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/app/uhhcraft", "healthcheck"]
ENTRYPOINT ["/app/uhhcraft"]
