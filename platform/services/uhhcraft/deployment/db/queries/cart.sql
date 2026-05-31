-- name: AddCartItem :one
INSERT INTO cart_items (
    user_id, session_id, item_type,
    catalog_item_id, generation_id,
    material_id, cut_finish_id,
    quantity, unit_price_usd, locked_until
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING *;

-- name: GetCartItemsByUser :many
SELECT * FROM cart_items
WHERE user_id = $1
ORDER BY created_at;

-- name: GetCartItemsBySession :many
SELECT * FROM cart_items
WHERE session_id = $1
ORDER BY created_at;

-- name: GetCartItemByID :one
SELECT * FROM cart_items WHERE id = $1 LIMIT 1;

-- name: RemoveCartItemByUser :exec
DELETE FROM cart_items
WHERE id = $1 AND user_id = $2;

-- name: RemoveCartItemBySession :exec
DELETE FROM cart_items
WHERE id = $1 AND session_id = $2;

-- name: ClearCartByUser :exec
DELETE FROM cart_items WHERE user_id = $1;

-- name: ClearCartBySession :exec
DELETE FROM cart_items WHERE session_id = $1;

-- name: LockCartItem :exec
UPDATE cart_items
SET locked_until = $2
WHERE id = $1;

-- name: CountCartItemsByUser :one
SELECT COUNT(*) FROM cart_items WHERE user_id = $1;

-- name: CountCartItemsBySession :one
SELECT COUNT(*) FROM cart_items WHERE session_id = $1;

-- name: MigrateGuestCartToUser :exec
UPDATE cart_items
SET user_id = $2, session_id = NULL
WHERE session_id = $1;
