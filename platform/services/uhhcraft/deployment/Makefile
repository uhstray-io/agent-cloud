.PHONY: dev watch build clean templ templ-watch css css-watch db-up db-down db-migrate db-rollback db-reset river-migrate sqlc lint fmt test test-integration e2e

# ── Config ───────────────────────────────────────────────────────────────────

BINARY     := bin/uhhcraft
MAIN       := ./cmd/server
TEMPL_DIR  := ./web/templates
CSS_IN     := ./web/static/css/input.css
CSS_OUT    := ./web/static/css/app.css
MIGRATIONS := ./db/migrations

# ── Development ──────────────────────────────────────────────────────────────

dev: templ-watch css-watch
	air

# Hot reload: watches Go + templ files, rebuilds on change
# Requires: go install github.com/air-verse/air@latest
watch:
	air

# Generate templ files (run once, then use templ-watch)
templ:
	templ generate

# Watch and regenerate templ files on change
templ-watch:
	templ generate --watch &

# Compile Tailwind CSS (one-shot)
css:
	tailwindcss -i $(CSS_IN) -o $(CSS_OUT) --minify

# Watch and recompile CSS on change
css-watch:
	tailwindcss -i $(CSS_IN) -o $(CSS_OUT) --watch &

# ── Build ────────────────────────────────────────────────────────────────────

build: templ css
	go build -ldflags="-s -w" -o $(BINARY) $(MAIN)

# ── Code generation ──────────────────────────────────────────────────────────

sqlc:
	sqlc generate

# ── Database ─────────────────────────────────────────────────────────────────

# Start local Postgres + Redis + MinIO via Docker Compose
db-up:
	docker compose up -d

db-down:
	docker compose down

# Run all pending migrations
db-migrate:
	goose -dir $(MIGRATIONS) postgres "$(DATABASE_URL)" up

# Roll back last migration
db-rollback:
	goose -dir $(MIGRATIONS) postgres "$(DATABASE_URL)" down

# Wipe and re-migrate (dev only)
db-reset:
	goose -dir $(MIGRATIONS) postgres "$(DATABASE_URL)" reset
	$(MAKE) db-migrate

# Run River migrations (job queue schema).
# Use the app binary's own subcommand — NOT the standalone `river` CLI — so the
# migrator version matches what prod runs via post-deploy.sh (they can diverge).
river-migrate:
	DATABASE_URL="$(DATABASE_URL)" go run $(MAIN) river migrate-up

# ── Quality ──────────────────────────────────────────────────────────────────

lint:
	golangci-lint run ./...

fmt:
	gofmt -w .
	goimports -w .

# ── Tests ────────────────────────────────────────────────────────────────────

test:
	go test -race -count=1 ./...

# Integration tests (requires running Postgres + Redis)
test-integration:
	go test -race -count=1 -tags integration ./...

# E2E tests via Playwright (requires running app on :3000)
e2e:
	npx playwright test

# ── Cleanup ──────────────────────────────────────────────────────────────────

clean:
	rm -rf $(BINARY) $(CSS_OUT)
	find . -name "*_templ.go" -delete
