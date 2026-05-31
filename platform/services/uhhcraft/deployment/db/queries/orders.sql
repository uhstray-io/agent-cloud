-- name: CreateOrder :one
INSERT INTO orders (
    user_id, guest_email,
    subtotal_usd, shipping_usd, tax_usd, discount_usd, discount_pct, total_usd,
    shipping_address, shipping_method,
    priority, age_gate_confirmed
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
RETURNING *;

-- name: CreateOrderItem :one
INSERT INTO order_items (
    order_id, item_type,
    catalog_item_id, generation_id,
    name, material_id, cut_finish_id,
    quantity, unit_price_usd, manufacturing_asset_path
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING *;

-- name: GetOrderByID :one
SELECT * FROM orders WHERE id = $1 LIMIT 1;

-- name: GetOrderByIDAndUser :one
SELECT * FROM orders WHERE id = $1 AND user_id = $2 LIMIT 1;

-- name: GetOrderByPaymentIntent :one
SELECT * FROM orders
WHERE stripe_payment_intent_id = $1
LIMIT 1;

-- name: GetOrderItemsByOrder :many
SELECT * FROM order_items WHERE order_id = $1 ORDER BY created_at;

-- name: ListOrdersByUser :many
SELECT * FROM orders
WHERE user_id = $1
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;

-- name: UpdateOrderStatus :exec
-- Terminal states ('delivered','cancelled','refunded') are immutable — once an
-- order reaches one, no further status change is accepted, so a late/duplicate
-- event can't regress it.
UPDATE orders
SET status = $2, updated_at = NOW()
WHERE id = $1
  AND status NOT IN ('delivered','cancelled','refunded');

-- name: UpdateOrderStripeIDs :exec
-- Only the initial 'pending' -> 'paid' transition is allowed here; a webhook
-- redelivery against an already-advanced order is a no-op (won't downgrade a
-- shipped/delivered order back to 'paid').
UPDATE orders
SET stripe_payment_intent_id = $2,
    stripe_charge_id = $3,
    status = 'paid',
    updated_at = NOW()
WHERE id = $1
  AND status = 'pending';

-- name: UpdateOrderTracking :exec
-- Forward-only: an order can only be marked shipped from 'paid' or
-- 'manufacturing', never from a terminal or earlier state.
UPDATE orders
SET tracking_number = $2,
    status = 'shipped',
    updated_at = NOW()
WHERE id = $1
  AND status IN ('paid','manufacturing');

-- name: UpdateOrderFulfillment :exec
UPDATE orders
SET fulfillment_route = $2,
    external_order_id = $3,
    updated_at = NOW()
WHERE id = $1;
