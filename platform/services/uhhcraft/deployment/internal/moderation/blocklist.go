// Package moderation pre-screens generation prompts against a keyword blocklist.
package moderation

import (
	"context"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Blocklist holds an in-memory copy of the prompt blocklist, refreshed periodically.
type Blocklist struct {
	mu      sync.RWMutex
	terms   []string
	db      *pgxpool.Pool
	refresh time.Duration
}

// New loads the blocklist from Postgres and starts a background refresh.
func New(ctx context.Context, db *pgxpool.Pool) (*Blocklist, error) {
	bl := &Blocklist{db: db, refresh: 5 * time.Minute}
	if err := bl.load(ctx); err != nil {
		return nil, err
	}
	go bl.refreshLoop(ctx)
	return bl, nil
}

func (bl *Blocklist) load(ctx context.Context) error {
	rows, err := bl.db.Query(ctx, `SELECT term FROM prompt_blocklist WHERE active = TRUE`)
	if err != nil {
		return err
	}
	defer rows.Close()

	var terms []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err != nil {
			return err
		}
		terms = append(terms, strings.ToLower(t))
	}

	bl.mu.Lock()
	bl.terms = terms
	bl.mu.Unlock()
	return rows.Err()
}

func (bl *Blocklist) refreshLoop(ctx context.Context) {
	ticker := time.NewTicker(bl.refresh)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := bl.load(ctx); err != nil {
				// Log but keep serving the last-good in-memory terms. Silent
				// failure here would let the blocklist drift stale invisibly.
				slog.Error("blocklist refresh failed", "err", err)
			}
		}
	}
}

// Check returns (blocked, matchedTerm) for the given prompt.
// The check is case-insensitive and matches whole words or substrings.
func (bl *Blocklist) Check(prompt string) (bool, string) {
	lower := strings.ToLower(prompt)

	bl.mu.RLock()
	defer bl.mu.RUnlock()

	for _, term := range bl.terms {
		if strings.Contains(lower, term) {
			return true, term
		}
	}
	return false, ""
}

// BlockedResponse is the user-facing message shown when a prompt is blocked.
const BlockedResponse = "We can't make that one — try something original! The best designs come from your own imagination."
