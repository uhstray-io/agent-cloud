-- name: CreateEmailVerificationToken :one
INSERT INTO email_verification_tokens (user_id, token, expires_at)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetEmailVerificationToken :one
-- Read-only lookup for validating a token without consuming it (e.g. rendering
-- a form). For verification, use ConsumeEmailVerificationToken instead.
SELECT * FROM email_verification_tokens
WHERE token = $1
  AND used_at IS NULL
  AND expires_at > NOW()
LIMIT 1;

-- name: MarkEmailVerificationTokenUsed :exec
UPDATE email_verification_tokens
SET used_at = NOW()
WHERE id = $1;

-- name: ConsumeEmailVerificationToken :one
-- Atomically mark a still-valid token used and return its user_id. A single
-- UPDATE ... RETURNING closes the replay window that a separate SELECT + UPDATE
-- leaves open: only one concurrent caller wins the row.
UPDATE email_verification_tokens
SET used_at = NOW()
WHERE token = $1
  AND used_at IS NULL
  AND expires_at > NOW()
RETURNING user_id;

-- name: CreatePasswordResetToken :one
INSERT INTO password_reset_tokens (user_id, token, expires_at)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetPasswordResetToken :one
-- Read-only lookup for validating a token without consuming it (e.g. rendering
-- the reset form). For the actual reset, use ConsumePasswordResetToken.
SELECT * FROM password_reset_tokens
WHERE token = $1
  AND used_at IS NULL
  AND expires_at > NOW()
LIMIT 1;

-- name: MarkPasswordResetTokenUsed :exec
UPDATE password_reset_tokens
SET used_at = NOW()
WHERE id = $1;

-- name: ConsumePasswordResetToken :one
-- Atomically mark a still-valid token used and return its user_id, closing the
-- replay window between validation and use.
UPDATE password_reset_tokens
SET used_at = NOW()
WHERE token = $1
  AND used_at IS NULL
  AND expires_at > NOW()
RETURNING user_id;
