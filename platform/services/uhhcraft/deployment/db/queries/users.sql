-- name: CreateUser :one
INSERT INTO users (email, password_hash, role)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetUserByEmail :one
SELECT * FROM users
WHERE email = $1
LIMIT 1;

-- name: GetUserByID :one
SELECT * FROM users
WHERE id = $1
LIMIT 1;

-- name: UpdateUserEmailVerified :exec
UPDATE users
SET email_verified = TRUE, updated_at = NOW()
WHERE id = $1;

-- name: UpdateUserPassword :exec
UPDATE users
SET password_hash = $2, updated_at = NOW()
WHERE id = $1;

-- name: UpdateUserDiscount :exec
UPDATE users
SET next_order_discount_pct = $2, updated_at = NOW()
WHERE id = $1;

-- name: ClearUserDiscount :exec
UPDATE users
SET next_order_discount_pct = 0, updated_at = NOW()
WHERE id = $1;
