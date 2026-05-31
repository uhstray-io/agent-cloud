// Package generation handles AI generation jobs via River.
package generation

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/riverqueue/river"
)

// ── Job argument types ────────────────────────────────────────────────────────

// GenerateImageArgs is the River job payload for sticker image generation.
type GenerateImageArgs struct {
	GenerationID string `json:"generation_id"`
	Prompt       string `json:"prompt"`
	MaterialID   string `json:"material_id"`
	CutFinishID  string `json:"cut_finish_id"`
}

func (GenerateImageArgs) Kind() string { return "generate_image" }

// Generate3DArgs is the River job payload for 3D model generation.
type Generate3DArgs struct {
	GenerationID string `json:"generation_id"`
	Prompt       string `json:"prompt"`
	MaterialID   string `json:"material_id"`
	FinishID     string `json:"finish_id"`
}

func (Generate3DArgs) Kind() string { return "generate_3d" }

// ── Image generation worker ───────────────────────────────────────────────────

// ImageWorker processes GenerateImageArgs jobs.
type ImageWorker struct {
	river.WorkerDefaults[GenerateImageArgs]
	db             *pgxpool.Pool
	imageServiceURL string
}

func NewImageWorker(db *pgxpool.Pool, imageServiceURL string) *ImageWorker {
	return &ImageWorker{db: db, imageServiceURL: imageServiceURL}
}

func (w *ImageWorker) Work(ctx context.Context, job *river.Job[GenerateImageArgs]) error {
	args := job.Args

	// Mark as processing
	if err := updateGenerationStatus(ctx, w.db, args.GenerationID, "processing", ""); err != nil {
		return err
	}

	// Call the Python image generation service
	reqBody, _ := json.Marshal(map[string]any{
		"generation_id": args.GenerationID,
		"prompt":        args.Prompt,
		"width":         1024,
		"height":        1024,
		"steps":         4, // Flux Schnell optimal
	})

	reqCtx, cancel := context.WithTimeout(ctx, 3*time.Minute)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost,
		w.imageServiceURL+"/generate", bytes.NewReader(reqBody))
	if err != nil {
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed", "request build error")
		return fmt.Errorf("image request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		// The request context may already be cancelled (deadline); use a
		// detached context so the terminal "failed" status is still recorded.
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed", "image service unreachable")
		return fmt.Errorf("image service: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed",
			fmt.Sprintf("image service status %d", resp.StatusCode))
		return fmt.Errorf("image service status %d: %s", resp.StatusCode, string(body))
	}

	// Normalized sidecar contract: comfyui returns the public, Caddy-routed
	// asset URL in "url".
	var result struct {
		URL string `json:"url"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed", "invalid response from image service")
		return err
	}

	// For stickers: the PNG is the image; GLB is a client-side Three.js mockup.
	return updateGenerationComplete(reqCtx, w.db, args.GenerationID, result.URL, "", "")
}

// ── 3D generation worker ──────────────────────────────────────────────────────

// ThreeDWorker processes Generate3DArgs jobs.
type ThreeDWorker struct {
	river.WorkerDefaults[Generate3DArgs]
	db              *pgxpool.Pool
	threeDServiceURL string
}

func NewThreeDWorker(db *pgxpool.Pool, threeDServiceURL string) *ThreeDWorker {
	return &ThreeDWorker{db: db, threeDServiceURL: threeDServiceURL}
}

func (w *ThreeDWorker) Work(ctx context.Context, job *river.Job[Generate3DArgs]) error {
	args := job.Args

	if err := updateGenerationStatus(ctx, w.db, args.GenerationID, "processing", ""); err != nil {
		return err
	}

	reqBody, _ := json.Marshal(map[string]any{
		"generation_id":      args.GenerationID,
		"prompt":             args.Prompt,
		"steps":              30,
		"guidance":           7.5,
		"octree_resolution":  256,
	})

	reqCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost,
		w.threeDServiceURL+"/generate", bytes.NewReader(reqBody))
	if err != nil {
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed", "request build error")
		return fmt.Errorf("3d request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed", "3d service unreachable")
		return fmt.Errorf("3d service: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed",
			fmt.Sprintf("3d service status %d", resp.StatusCode))
		return fmt.Errorf("3d service status %d: %s", resp.StatusCode, string(body))
	}

	// Normalized sidecar contract: hunyuan3d returns public, Caddy-routed URLs.
	var result struct {
		GLBURL string `json:"glb_url"`
		STLURL string `json:"stl_url"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		_ = updateGenerationStatus(context.Background(), w.db, args.GenerationID, "failed", "invalid response from 3d service")
		return err
	}

	return updateGenerationComplete(reqCtx, w.db, args.GenerationID, "", result.GLBURL, result.STLURL)
}

// ── DB helpers ────────────────────────────────────────────────────────────────

func updateGenerationStatus(ctx context.Context, db *pgxpool.Pool, id, status, errMsg string) error {
	_, err := db.Exec(ctx,
		`UPDATE generations SET status = $2, error_message = NULLIF($3,'') WHERE id = $1`,
		id, status, errMsg,
	)
	return err
}

func updateGenerationComplete(ctx context.Context, db *pgxpool.Pool, id, pngPath, glbPath, stlPath string) error {
	_, err := db.Exec(ctx,
		`UPDATE generations
		 SET status = 'completed',
		     asset_png_path = NULLIF($2,''),
		     asset_glb_path = NULLIF($3,''),
		     asset_stl_path = NULLIF($4,''),
		     error_message  = NULL,
		     completed_at   = NOW()
		 WHERE id = $1`,
		id, pngPath, glbPath, stlPath,
	)
	return err
}

// RegisterWorkers adds all generation workers to a River worker pool.
func RegisterWorkers(workers *river.Workers, db *pgxpool.Pool, imageURL, threeDURL string) {
	river.AddWorker(workers, NewImageWorker(db, imageURL))
	river.AddWorker(workers, NewThreeDWorker(db, threeDURL))
}
