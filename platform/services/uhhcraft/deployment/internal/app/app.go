package app

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/alexedwards/scs/pgxstore"
	"github.com/alexedwards/scs/v2"
	pgx "github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"github.com/riverqueue/river"
	"github.com/riverqueue/river/riverdriver/riverpgxv5"
	"github.com/stripe/stripe-go/v79"

	"github.com/wisward/uhhcraft/internal/config"
	"github.com/wisward/uhhcraft/internal/db"
	"github.com/wisward/uhhcraft/internal/discord"
	"github.com/wisward/uhhcraft/internal/email"
	"github.com/wisward/uhhcraft/internal/moderation"
	"github.com/wisward/uhhcraft/internal/ratelimit"
	"github.com/wisward/uhhcraft/internal/storage"
	"github.com/wisward/uhhcraft/web/templates/layouts"
)

// App holds all application-level dependencies.
// Passed to handlers so they can access services without global state.
type App struct {
	Config       *config.Config
	DB           *pgxpool.Pool
	Redis        *redis.Client
	Sessions     *scs.SessionManager
	River        *river.Client[pgx.Tx]
	RiverWorkers *river.Workers
	Logger       *slog.Logger
	Email        *email.Client
	Discord      *discord.Client
	RateLimit    *ratelimit.Limiter
	Storage      *storage.Client
	Blocklist    *moderation.Blocklist
	Queries      *db.Queries

	// bgCancel stops background loops (e.g. the blocklist refresh ticker)
	// started during New; called by Close.
	bgCancel context.CancelFunc
}

// New initialises all dependencies and returns a ready App.
func New(cfg *config.Config) (*App, error) {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))

	// Expose the public origin to the layout package so templates can build
	// absolute URLs (OG tags) without hardcoding the domain.
	layouts.SiteBaseURL = cfg.App.BaseURL

	// ── Postgres ─────────────────────────────────────────────────────────────
	poolCfg, err := pgxpool.ParseConfig(cfg.DB.URL)
	if err != nil {
		return nil, fmt.Errorf("parse db url: %w", err)
	}
	poolCfg.MaxConns = 20
	poolCfg.MinConns = 2
	poolCfg.MaxConnLifetime = 30 * time.Minute
	poolCfg.MaxConnIdleTime = 5 * time.Minute

	pool, err := pgxpool.NewWithConfig(context.Background(), poolCfg)
	if err != nil {
		return nil, fmt.Errorf("connect to postgres: %w", err)
	}
	// Bound the startup ping so a half-up network can't hang the boot forever.
	pingCtx, pingCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer pingCancel()
	if pingErr := pool.Ping(pingCtx); pingErr != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", pingErr)
	}
	logger.Info("postgres connected")

	// ── Redis ─────────────────────────────────────────────────────────────────
	redisOpts, err := redis.ParseURL(cfg.Redis.URL)
	if err != nil {
		pool.Close()
		return nil, fmt.Errorf("parse redis url: %w", err)
	}
	rdb := redis.NewClient(redisOpts)
	redisPingCtx, redisPingCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer redisPingCancel()
	if pingErr := rdb.Ping(redisPingCtx).Err(); pingErr != nil {
		_ = rdb.Close()
		pool.Close()
		return nil, fmt.Errorf("ping redis: %w", pingErr)
	}
	logger.Info("redis connected")

	// ── Sessions ──────────────────────────────────────────────────────────────
	sessions := scs.New()
	sessions.Store = pgxstore.New(pool)
	sessions.Lifetime = 30 * 24 * time.Hour
	sessions.Cookie.HttpOnly = true
	sessions.Cookie.Secure = cfg.App.Env == "production"
	sessions.Cookie.SameSite = http.SameSiteLaxMode

	// ── Stripe ────────────────────────────────────────────────────────────────
	stripe.Key = cfg.Stripe.SecretKey

	// ── River (job queue) ─────────────────────────────────────────────────────
	// Workers are registered externally (in internal/server) to avoid import cycles.
	workers := river.NewWorkers()

	riverClient, err := river.NewClient(riverpgxv5.New(pool), &river.Config{
		Workers: workers,
		Queues: map[string]river.QueueConfig{
			river.QueueDefault: {MaxWorkers: 10},
			"ai_generation":    {MaxWorkers: 3},
			"email":            {MaxWorkers: 5},
		},
		Logger: logger,
	})
	if err != nil {
		_ = rdb.Close()
		pool.Close()
		return nil, fmt.Errorf("init river: %w", err)
	}
	logger.Info("river initialised")

	// ── Storage (MinIO) ───────────────────────────────────────────────────────
	store, err := storage.New(&cfg.Storage)
	if err != nil {
		_ = rdb.Close()
		pool.Close()
		return nil, fmt.Errorf("init storage: %w", err)
	}

	// ── Content moderation blocklist ──────────────────────────────────────────
	// Background loops get a cancellable context so Close can stop them; the
	// blocklist refresh ticker would otherwise outlive the pool it queries.
	bgCtx, bgCancel := context.WithCancel(context.Background())
	var bl *moderation.Blocklist
	bl, err = moderation.New(bgCtx, pool)
	if err != nil {
		logger.Warn("blocklist init failed (table may not exist yet — run migrations)", "err", err)
		bl = nil
	}

	return &App{
		Config:       cfg,
		DB:           pool,
		Redis:        rdb,
		Sessions:     sessions,
		River:        riverClient,
		RiverWorkers: workers,
		Logger:       logger,
		Email:        email.New(cfg),
		Discord:      discord.New(cfg.Discord.OrdersWebhookURL, cfg.Discord.OpsWebhookURL),
		RateLimit:    ratelimit.New(rdb),
		Storage:      store,
		Blocklist:    bl,
		Queries:      db.New(pool),
		bgCancel:     bgCancel,
	}, nil
}

// Close shuts down all connections and background loops gracefully.
func (a *App) Close() {
	if a.bgCancel != nil {
		a.bgCancel() // stop the blocklist refresh loop before closing the pool
	}
	a.DB.Close()
	_ = a.Redis.Close()
}

// IsAdmin reports whether the currently authenticated user has the admin role.
func (a *App) IsAdmin(r *http.Request) bool {
	role, _ := a.Sessions.Get(r.Context(), "user_role").(string)
	return role == "admin"
}

// UserID returns the authenticated user's UUID, or "" for guests.
func (a *App) UserID(r *http.Request) string {
	id, _ := a.Sessions.Get(r.Context(), "user_id").(string)
	return id
}

// IsAuthenticated reports whether a user session is active.
func (a *App) IsAuthenticated(r *http.Request) bool {
	return a.UserID(r) != ""
}
