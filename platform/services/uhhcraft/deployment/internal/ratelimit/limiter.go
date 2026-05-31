// Package ratelimit provides Redis-backed rate limiting for generation requests.
package ratelimit

import (
	"context"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

// Limiter performs per-key sliding-window rate limiting backed by Redis.
type Limiter struct {
	rdb *redis.Client
}

// New returns a Limiter using the given Redis client.
func New(rdb *redis.Client) *Limiter {
	return &Limiter{rdb: rdb}
}

// GenerationKey returns the Redis key for a generation rate limit.
// Authenticated users are keyed by user ID; guests by session ID.
func GenerationKey(id string, isUser bool) string {
	if isUser {
		return fmt.Sprintf("gen_rate:user:%s", id)
	}
	return fmt.Sprintf("gen_rate:session:%s", id)
}

// GenerationCooldown is the wait period between generation attempts.
// Admin users bypass this entirely (checked at the handler level).
const (
	GuestCooldown   = 90 * time.Second  // guests wait longer
	AccountCooldown = 20 * time.Second  // account holders wait less
)

// Result is returned by Allow.
type Result struct {
	Allowed       bool
	RemainingWait time.Duration // how long until the next attempt is allowed
}

// Allow checks whether the given key may make a generation request.
// If allowed, it records the attempt (sets a TTL key in Redis).
// If not allowed, it returns the remaining cooldown duration.
//
// The write is a single atomic SET ... NX EX (SetNX): only the first of N
// concurrent callers wins, so the cooldown can't be defeated by a
// check-then-set race on the hot path. The remaining wait is read back only
// when the write was rejected.
func (l *Limiter) Allow(ctx context.Context, key string, cooldown time.Duration) (Result, error) {
	ok, err := l.rdb.SetNX(ctx, key, 1, cooldown).Result()
	if err != nil {
		return Result{}, fmt.Errorf("ratelimit setnx: %w", err)
	}
	if ok {
		return Result{Allowed: true}, nil
	}

	// Write rejected — a cooldown key already exists. Report the remaining wait.
	ttl, err := l.rdb.TTL(ctx, key).Result()
	if err != nil && err != redis.Nil {
		return Result{}, fmt.Errorf("ratelimit ttl: %w", err)
	}
	if ttl < 0 {
		ttl = 0
	}
	return Result{Allowed: false, RemainingWait: ttl}, nil
}

// Reset removes the rate limit key (used by admin bypass and tests).
func (l *Limiter) Reset(ctx context.Context, key string) error {
	return l.rdb.Del(ctx, key).Err()
}

// LoginKey returns the Redis key for login attempt rate limiting (per IP).
func LoginKey(ip string) string {
	return fmt.Sprintf("login_rate:ip:%s", ip)
}

// AllowLogin checks whether the IP may attempt a login.
// Returns false after 5 failed attempts within 15 minutes.
func (l *Limiter) AllowLogin(ctx context.Context, ip string) (bool, error) {
	key := LoginKey(ip)
	count, err := l.rdb.Incr(ctx, key).Result()
	if err != nil {
		return false, fmt.Errorf("login rate incr: %w", err)
	}
	if count == 1 {
		// First attempt in this window — set expiry. If this fails the key
		// would otherwise never expire and the IP would be blocked forever,
		// so surface the error to the caller.
		if err := l.rdb.Expire(ctx, key, 15*time.Minute).Err(); err != nil {
			return false, fmt.Errorf("login rate expire: %w", err)
		}
	}
	return count <= 5, nil
}

// ResetLogin clears the login rate limit for an IP (call on successful login).
// Returns an error so the caller knows whether the throttle was actually cleared.
func (l *Limiter) ResetLogin(ctx context.Context, ip string) error {
	if err := l.rdb.Del(ctx, LoginKey(ip)).Err(); err != nil {
		return fmt.Errorf("login rate reset: %w", err)
	}
	return nil
}
