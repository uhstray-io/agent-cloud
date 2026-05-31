-- name: CreateGeneration :one
INSERT INTO generations (user_id, session_id, product_type, prompt, material_id, cut_finish_id)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: GetGenerationByID :one
SELECT * FROM generations WHERE id = $1 LIMIT 1;

-- name: UpdateGenerationStatus :exec
UPDATE generations
SET status = $2, error_message = $3
WHERE id = $1;

-- name: UpdateGenerationRiverJob :exec
UPDATE generations
SET river_job_id = $2
WHERE id = $1;

-- name: UpdateGenerationComplete :exec
UPDATE generations
SET status = 'completed',
    asset_png_path = $2,
    asset_glb_path = $3,
    asset_stl_path = $4,
    error_message  = NULL,
    completed_at   = NOW()
WHERE id = $1;

-- name: UpdateGenerationFailed :exec
UPDATE generations
SET status = 'failed', error_message = $2
WHERE id = $1;

-- name: ListUserRecentGenerations :many
SELECT * FROM generations
WHERE user_id = $1
ORDER BY created_at DESC
LIMIT 10;

-- name: ListSessionRecentGenerations :many
SELECT * FROM generations
WHERE session_id = $1
ORDER BY created_at DESC
LIMIT 3;

-- PurgeOldUserGenerations deletes all but the 10 most recent for a user.
-- In-flight generations ('pending'/'processing') are never purged even if they
-- fall outside the 10 newest — a running River job still references them, and
-- deleting the row would orphan the job and lose the asset on completion.
-- name: PurgeOldUserGenerations :exec
DELETE FROM generations
WHERE user_id = $1
  AND status NOT IN ('pending','processing')
  AND id NOT IN (
      SELECT id FROM generations
      WHERE user_id = $1
      ORDER BY created_at DESC
      LIMIT 10
  );
