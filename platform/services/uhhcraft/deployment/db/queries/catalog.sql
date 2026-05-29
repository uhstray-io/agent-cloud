-- name: ListCatalogCategories :many
SELECT * FROM catalog_categories
WHERE active = TRUE
ORDER BY sort_order, name;

-- name: GetCategoryBySlug :one
SELECT * FROM catalog_categories
WHERE slug = $1 AND active = TRUE
LIMIT 1;

-- name: ListCatalogItems :many
SELECT * FROM catalog_items
WHERE active = TRUE
ORDER BY created_at DESC
LIMIT $1 OFFSET $2;

-- name: ListCatalogItemsByType :many
SELECT * FROM catalog_items
WHERE product_type = $1 AND active = TRUE
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;

-- name: ListCatalogItemsByCategory :many
SELECT ci.* FROM catalog_items ci
JOIN catalog_categories cc ON cc.id = ci.category_id
WHERE cc.slug = $1 AND ci.active = TRUE
ORDER BY ci.created_at DESC
LIMIT $2 OFFSET $3;

-- name: GetCatalogItemBySlug :one
SELECT * FROM catalog_items
WHERE slug = $1 AND active = TRUE
LIMIT 1;

-- name: GetCatalogItemByID :one
-- Honors hidden state: an inactive item is not purchasable or linkable, so it
-- must not be fetchable by id either (prevents deep-linking to a pulled item).
SELECT * FROM catalog_items
WHERE id = $1 AND active = TRUE
LIMIT 1;

-- name: CountCatalogItems :one
SELECT COUNT(*) FROM catalog_items WHERE active = TRUE;

-- name: ListShowcaseItems :many
SELECT
    si.id,
    si.caption,
    si.sort_order,
    ci.id         AS catalog_item_id,
    ci.slug       AS item_slug,
    ci.name       AS item_name,
    ci.product_type,
    ci.thumbnail_path
FROM showcase_items si
JOIN catalog_items ci ON ci.id = si.catalog_item_id
WHERE si.active = TRUE AND ci.active = TRUE
ORDER BY si.sort_order
LIMIT 12;
