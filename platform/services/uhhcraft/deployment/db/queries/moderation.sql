-- name: ListActiveBlocklistTerms :many
SELECT term FROM prompt_blocklist
WHERE active = TRUE
ORDER BY term;

-- name: AddBlocklistTerm :one
INSERT INTO prompt_blocklist (category, term)
VALUES ($1, $2)
ON CONFLICT (term) DO UPDATE SET active = TRUE, category = $1
RETURNING *;

-- name: DeactivateBlocklistTerm :exec
UPDATE prompt_blocklist
SET active = FALSE
WHERE id = $1;
