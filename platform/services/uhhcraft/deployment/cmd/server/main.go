package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/joho/godotenv"
	"github.com/riverqueue/river/riverdriver/riverpgxv5"
	"github.com/riverqueue/river/rivermigrate"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/config"
	"github.com/wisward/uhhcraft/internal/server"
)

// GitSHA is set at build time via -ldflags="-X main.GitSHA=..."
var GitSHA = "dev"

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "healthcheck":
			os.Exit(runHealthcheck())
		case "river":
			if len(os.Args) > 2 && os.Args[2] == "migrate-up" {
				os.Exit(runRiverMigrateUp())
			}
			fmt.Fprintln(os.Stderr, "usage: uhhcraft river migrate-up")
			os.Exit(2)
		case "version":
			fmt.Println(GitSHA)
			os.Exit(0)
		}
	}
	runServer()
}

// runHealthcheck is invoked by the Dockerfile HEALTHCHECK directive.
// Performs a local HTTP GET on /healthz and exits 0 on 200, 1 otherwise.
// Intentionally cheap: no DB / Redis / MinIO probes, just confirms the
// HTTP server in this process is accepting connections.
func runHealthcheck() int {
	_ = godotenv.Load()
	port := os.Getenv("APP_PORT")
	if port == "" {
		port = "3000"
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%s/healthz", port))
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: %v\n", err)
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "healthcheck: status %d\n", resp.StatusCode)
		return 1
	}
	return 0
}

// runRiverMigrateUp applies River's own migrations (river_job,
// river_leader, river_migration, ...). Invoked by post-deploy.sh after
// goose has applied the application's schema migrations. Idempotent —
// River's migrator skips already-applied migrations.
func runRiverMigrateUp() int {
	_ = godotenv.Load()
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		fmt.Fprintln(os.Stderr, "river migrate-up: DATABASE_URL not set")
		return 1
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		fmt.Fprintf(os.Stderr, "river migrate-up: pgxpool: %v\n", err)
		return 1
	}
	defer pool.Close()
	migrator := rivermigrate.New(riverpgxv5.New(pool), nil)
	res, err := migrator.Migrate(ctx, rivermigrate.DirectionUp, nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "river migrate-up: %v\n", err)
		return 1
	}
	fmt.Printf("river migrate-up: %d migration(s) applied\n", len(res.Versions))
	return 0
}

func runServer() {
	_ = godotenv.Load()

	cfg, err := config.Load("./config")
	if err != nil {
		slog.Error("failed to load config", "err", err)
		os.Exit(1)
	}

	application, err := app.New(cfg)
	if err != nil {
		slog.Error("failed to init app", "err", err)
		os.Exit(1)
	}
	defer application.Close()

	server.RegisterWorkers(application)

	if err := application.River.Start(context.Background()); err != nil {
		slog.Error("failed to start river", "err", err)
		os.Exit(1)
	}

	router := server.Routes(application)

	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", cfg.App.Port),
		Handler:      router,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	// Listener errors flow back here rather than calling os.Exit inside the
	// goroutine, which would bypass `defer application.Close()` and the River
	// shutdown path below.
	serverErr := make(chan error, 1)
	go func() {
		application.Logger.Info("server starting", "addr", srv.Addr, "env", cfg.App.Env, "sha", GitSHA)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			serverErr <- err
		}
	}()

	select {
	case <-quit:
		application.Logger.Info("shutting down...")
	case err := <-serverErr:
		application.Logger.Error("server error", "err", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if err := application.River.Stop(ctx); err != nil {
		application.Logger.Error("river stop error", "err", err)
	}
	if err := srv.Shutdown(ctx); err != nil {
		application.Logger.Error("server shutdown error", "err", err)
	}

	application.Logger.Info("shutdown complete")
}
