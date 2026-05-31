// Package db provides typed database query helpers backed by pgx/v5.
// These are hand-written to match the SQL in db/queries/*.sql.
// Replace with sqlc-generated code once sqlc is added to the toolchain.
package db

import (
	"context"
	"encoding/hex"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

// UUIDToString formats a pgtype.UUID as a lowercase hyphenated UUID string.
// pgtype.UUID has no String() method in pgx v5.
func UUIDToString(u pgtype.UUID) string {
	if !u.Valid {
		return ""
	}
	b := u.Bytes
	var buf [36]byte
	hex.Encode(buf[0:8], b[0:4])
	buf[8] = '-'
	hex.Encode(buf[9:13], b[4:6])
	buf[13] = '-'
	hex.Encode(buf[14:18], b[6:8])
	buf[18] = '-'
	hex.Encode(buf[19:23], b[8:10])
	buf[23] = '-'
	hex.Encode(buf[24:36], b[10:16])
	return string(buf[:])
}

// Queries wraps a pgxpool.Pool and exposes typed query methods.
type Queries struct {
	pool *pgxpool.Pool
}

// New returns a Queries backed by the given pool.
func New(pool *pgxpool.Pool) *Queries {
	return &Queries{pool: pool}
}

// ── User ──────────────────────────────────────────────────────────────────────

type User struct {
	ID                   pgtype.UUID
	Email                string
	PasswordHash         string
	Role                 string
	EmailVerified        bool
	NextOrderDiscountPct int
	CreatedAt            pgtype.Timestamptz
	UpdatedAt            pgtype.Timestamptz
}

type CreateUserParams struct {
	Email        string
	PasswordHash string
	Role         string
}

type UpdateUserPasswordParams struct {
	ID           pgtype.UUID
	PasswordHash string
}

const userColumns = `id, email, password_hash, role, email_verified, next_order_discount_pct, created_at, updated_at`

func scanUser(row interface{ Scan(...any) error }) (User, error) {
	var u User
	err := row.Scan(&u.ID, &u.Email, &u.PasswordHash, &u.Role, &u.EmailVerified,
		&u.NextOrderDiscountPct, &u.CreatedAt, &u.UpdatedAt)
	return u, err
}

func (q *Queries) GetUserByEmail(ctx context.Context, email string) (User, error) {
	row := q.pool.QueryRow(ctx,
		`SELECT `+userColumns+` FROM users WHERE email = $1`, email)
	return scanUser(row)
}

func (q *Queries) GetUserByID(ctx context.Context, id pgtype.UUID) (User, error) {
	row := q.pool.QueryRow(ctx,
		`SELECT `+userColumns+` FROM users WHERE id = $1`, id)
	return scanUser(row)
}

func (q *Queries) CreateUser(ctx context.Context, p CreateUserParams) (User, error) {
	row := q.pool.QueryRow(ctx,
		`INSERT INTO users (email, password_hash, role) VALUES ($1, $2, $3)
		 RETURNING `+userColumns,
		p.Email, p.PasswordHash, p.Role)
	return scanUser(row)
}

func (q *Queries) UpdateUserEmailVerified(ctx context.Context, id pgtype.UUID) error {
	_, err := q.pool.Exec(ctx,
		`UPDATE users SET email_verified = TRUE, updated_at = NOW() WHERE id = $1`, id)
	return err
}

func (q *Queries) UpdateUserPassword(ctx context.Context, p UpdateUserPasswordParams) error {
	_, err := q.pool.Exec(ctx,
		`UPDATE users SET password_hash = $2, updated_at = NOW() WHERE id = $1`,
		p.ID, p.PasswordHash)
	return err
}

func (q *Queries) UpdateUserDiscount(ctx context.Context, id pgtype.UUID, pct int) error {
	_, err := q.pool.Exec(ctx,
		`UPDATE users SET next_order_discount_pct = $2, updated_at = NOW() WHERE id = $1`, id, pct)
	return err
}

func (q *Queries) ClearUserDiscount(ctx context.Context, id pgtype.UUID) error {
	_, err := q.pool.Exec(ctx,
		`UPDATE users SET next_order_discount_pct = 0, updated_at = NOW() WHERE id = $1`, id)
	return err
}

// ── Email verification ────────────────────────────────────────────────────────

type EmailVerificationToken struct {
	ID        pgtype.UUID
	UserID    pgtype.UUID
	Token     string
	ExpiresAt pgtype.Timestamptz
	UsedAt    *pgtype.Timestamptz
}

type CreateEmailVerificationTokenParams struct {
	UserID    pgtype.UUID
	Token     string
	ExpiresAt pgtype.Timestamptz
}

func (q *Queries) CreateEmailVerificationToken(ctx context.Context, p CreateEmailVerificationTokenParams) (EmailVerificationToken, error) {
	var t EmailVerificationToken
	err := q.pool.QueryRow(ctx,
		`INSERT INTO email_verification_tokens (user_id, token, expires_at)
		 VALUES ($1, $2, $3)
		 RETURNING id, user_id, token, expires_at, used_at`,
		p.UserID, p.Token, p.ExpiresAt,
	).Scan(&t.ID, &t.UserID, &t.Token, &t.ExpiresAt, &t.UsedAt)
	return t, err
}

func (q *Queries) GetEmailVerificationToken(ctx context.Context, token string) (EmailVerificationToken, error) {
	var t EmailVerificationToken
	err := q.pool.QueryRow(ctx,
		`SELECT id, user_id, token, expires_at, used_at
		 FROM email_verification_tokens
		 WHERE token = $1 AND used_at IS NULL AND expires_at > NOW()`, token,
	).Scan(&t.ID, &t.UserID, &t.Token, &t.ExpiresAt, &t.UsedAt)
	return t, err
}

// ConsumeEmailVerificationToken atomically marks a still-valid token used and
// returns its user_id. A single UPDATE ... RETURNING closes the replay window
// that a separate SELECT-then-UPDATE leaves open: only one concurrent caller
// gets the row. Returns pgx.ErrNoRows if the token is missing, expired, or
// already used.
func (q *Queries) ConsumeEmailVerificationToken(ctx context.Context, token string) (pgtype.UUID, error) {
	var userID pgtype.UUID
	err := q.pool.QueryRow(ctx,
		`UPDATE email_verification_tokens
		 SET used_at = NOW()
		 WHERE token = $1 AND used_at IS NULL AND expires_at > NOW()
		 RETURNING user_id`, token,
	).Scan(&userID)
	return userID, err
}

// ── Password reset ────────────────────────────────────────────────────────────

type PasswordResetToken struct {
	ID        pgtype.UUID
	UserID    pgtype.UUID
	Token     string
	ExpiresAt pgtype.Timestamptz
	UsedAt    *pgtype.Timestamptz
}

type CreatePasswordResetTokenParams struct {
	UserID    pgtype.UUID
	Token     string
	ExpiresAt pgtype.Timestamptz
}

func (q *Queries) CreatePasswordResetToken(ctx context.Context, p CreatePasswordResetTokenParams) (PasswordResetToken, error) {
	var t PasswordResetToken
	err := q.pool.QueryRow(ctx,
		`INSERT INTO password_reset_tokens (user_id, token, expires_at)
		 VALUES ($1, $2, $3)
		 RETURNING id, user_id, token, expires_at, used_at`,
		p.UserID, p.Token, p.ExpiresAt,
	).Scan(&t.ID, &t.UserID, &t.Token, &t.ExpiresAt, &t.UsedAt)
	return t, err
}

func (q *Queries) GetPasswordResetToken(ctx context.Context, token string) (PasswordResetToken, error) {
	var t PasswordResetToken
	err := q.pool.QueryRow(ctx,
		`SELECT id, user_id, token, expires_at, used_at
		 FROM password_reset_tokens
		 WHERE token = $1 AND used_at IS NULL AND expires_at > NOW()`, token,
	).Scan(&t.ID, &t.UserID, &t.Token, &t.ExpiresAt, &t.UsedAt)
	return t, err
}

// ConsumePasswordResetToken atomically marks a still-valid token used and
// returns its user_id, closing the replay window between validation and use.
// Returns pgx.ErrNoRows if the token is missing, expired, or already used.
func (q *Queries) ConsumePasswordResetToken(ctx context.Context, token string) (pgtype.UUID, error) {
	var userID pgtype.UUID
	err := q.pool.QueryRow(ctx,
		`UPDATE password_reset_tokens
		 SET used_at = NOW()
		 WHERE token = $1 AND used_at IS NULL AND expires_at > NOW()
		 RETURNING user_id`, token,
	).Scan(&userID)
	return userID, err
}

// ── Cart ──────────────────────────────────────────────────────────────────────

type MigrateGuestCartToUserParams struct {
	SessionID pgtype.Text
	UserID    pgtype.UUID
}

func (q *Queries) MigrateGuestCartToUser(ctx context.Context, p MigrateGuestCartToUserParams) error {
	_, err := q.pool.Exec(ctx,
		`UPDATE cart_items SET user_id = $2, session_id = NULL
		 WHERE session_id = $1 AND user_id IS NULL`,
		p.SessionID, p.UserID)
	return err
}
