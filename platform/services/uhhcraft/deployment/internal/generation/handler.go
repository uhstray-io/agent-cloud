package generation

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/riverqueue/river"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/auth"
	"github.com/wisward/uhhcraft/internal/db"
	"github.com/wisward/uhhcraft/internal/ratelimit"
	"github.com/wisward/uhhcraft/web/templates/pages"
)

// FormHandler renders the /generate page.
func FormHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		isAuth := auth.IsAuthenticated(a.Sessions, r)
		userID := auth.UserID(a.Sessions, r)

		var recentGens []recentGenEntry
		if isAuth {
			rows, err := a.DB.Query(r.Context(),
				`SELECT id, prompt, product_type, asset_png_path, asset_glb_path, status
				 FROM generations WHERE user_id = $1 ORDER BY created_at DESC LIMIT 10`,
				userID,
			)
			if err == nil {
				defer rows.Close()
				for rows.Next() {
					var g recentGenEntry
					_ = rows.Scan(&g.ID, &g.Prompt, &g.ProductType, &g.AssetPNGPath, &g.AssetGLBPath, &g.Status)
					recentGens = append(recentGens, g)
				}
			}
		}

		_ = recentGens // recent generations are shown via the status/canvas flow
		renderGenerateForm(w, r, a, "")
	}
}

// SubmitHandler processes the generation form POST.
func SubmitHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}

		prompt := strings.TrimSpace(r.FormValue("prompt"))
		productType := r.FormValue("product_type") // "sticker" | "print"
		materialID := r.FormValue("material_id")
		cutFinishID := r.FormValue("cut_finish_id")

		// Basic validation
		if len(prompt) < 3 {
			respondError(w, r, "Your prompt needs to be a bit longer — describe what you'd like!", http.StatusUnprocessableEntity)
			return
		}
		if productType != "sticker" && productType != "print" {
			respondError(w, r, "Please choose a product type.", http.StatusUnprocessableEntity)
			return
		}

		// Validate material + cut/finish against the configured options for the
		// chosen product type. Reject arbitrary IDs the service can't price or
		// fulfill.
		if !validSelection(a, productType, materialID, cutFinishID) {
			respondError(w, r, "Please choose a valid material and finish.", http.StatusUnprocessableEntity)
			return
		}

		// Moderation check
		if a.Blocklist != nil {
			if blocked, _ := a.Blocklist.Check(prompt); blocked {
				respondError(w, r, "We can't make that one — try something original! The best designs come from your own imagination.", http.StatusUnprocessableEntity)
				return
			}
		}

		// Rate limiting
		isAuth := auth.IsAuthenticated(a.Sessions, r)
		isAdmin := auth.IsAdmin(a.Sessions, r)

		// Resolve a stable guest key for unauthenticated users. This persists
		// a value to the SCS session so the same client keeps the same key
		// across requests — otherwise a guest could dodge the cooldown by
		// simply re-requesting (no cookie was ever written back).
		guestID := ""
		if !isAuth {
			guestID = guestSessionID(a, r)
		}

		if !isAdmin {
			var cooldown time.Duration
			var rlKey string
			if isAuth {
				userID := auth.UserID(a.Sessions, r)
				cooldown = ratelimit.AccountCooldown
				rlKey = ratelimit.GenerationKey(userID, true)
			} else {
				cooldown = ratelimit.GuestCooldown
				rlKey = ratelimit.GenerationKey(guestID, false)
			}

			result, err := a.RateLimit.Allow(r.Context(), rlKey, cooldown)
			if err != nil {
				a.Logger.Error("rate limit check", "err", err)
			}
			if !result.Allowed {
				respondCooldown(w, r, result.RemainingWait, isAuth)
				return
			}
		}

		// Create generation record
		var userIDVal pgtype.UUID
		var sessionIDVal pgtype.Text

		if isAuth {
			uid := auth.UserID(a.Sessions, r)
			_ = userIDVal.Scan(uid)
		} else {
			sessionIDVal = pgtype.Text{String: guestID, Valid: true}
		}

		var genID pgtype.UUID
		err := a.DB.QueryRow(r.Context(),
			`INSERT INTO generations (user_id, session_id, product_type, prompt, material_id, cut_finish_id)
			 VALUES ($1, $2, $3, $4, $5, $6)
			 RETURNING id`,
			userIDVal, sessionIDVal, productType, prompt, materialID, cutFinishID,
		).Scan(&genID)
		if err != nil {
			a.Logger.Error("create generation", "err", err)
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}

		genIDStr := db.UUIDToString(genID)

		// Enqueue River job. If the insert fails the generation row would
		// otherwise sit in 'queued' forever and the canvas page would poll
		// indefinitely, so mark it failed and surface an error to the user.
		var jobID int64
		var insErr error
		if productType == "sticker" {
			insertResult, err := a.River.Insert(r.Context(), GenerateImageArgs{
				GenerationID: genIDStr,
				Prompt:       prompt,
				MaterialID:   materialID,
				CutFinishID:  cutFinishID,
			}, &river.InsertOpts{Queue: "ai_generation"})
			insErr = err
			if err == nil {
				jobID = insertResult.Job.ID
			}
		} else {
			insertResult, err := a.River.Insert(r.Context(), Generate3DArgs{
				GenerationID: genIDStr,
				Prompt:       prompt,
				MaterialID:   materialID,
				FinishID:     cutFinishID,
			}, &river.InsertOpts{Queue: "ai_generation"})
			insErr = err
			if err == nil {
				jobID = insertResult.Job.ID
			}
		}
		if insErr != nil {
			a.Logger.Error("enqueue generation job", "gen", genIDStr, "err", insErr)
			_, _ = a.DB.Exec(r.Context(),
				`UPDATE generations SET status = 'failed' WHERE id = $1`, genIDStr)
			respondError(w, r, "We couldn't start your generation — please try again in a moment.",
				http.StatusServiceUnavailable)
			return
		}

		// Store River job ID
		if jobID > 0 {
			_, _ = a.DB.Exec(r.Context(),
				`UPDATE generations SET river_job_id = $2 WHERE id = $1`,
				genIDStr, jobID,
			)
		}

		// Purge old generations for account users (keep last 10)
		if isAuth {
			uid := auth.UserID(a.Sessions, r)
			_, _ = a.DB.Exec(context.Background(),
				`DELETE FROM generations WHERE user_id = $1 AND id NOT IN (
				    SELECT id FROM generations WHERE user_id = $1 ORDER BY created_at DESC LIMIT 10
				)`, uid)
		}

		// Redirect to canvas page (which polls for completion)
		http.Redirect(w, r, "/canvas/"+genIDStr, http.StatusSeeOther)
	}
}

// StatusHandler returns the current status of a generation as JSON.
// Polled by the canvas page via HTMX or fetch.
func StatusHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := chi.URLParam(r, "id")

		// Authorize: a generation is visible only to its owner (the
		// authenticated account) or the guest session that created it. This
		// stops a guessed/leaked ID from exposing another user's generation.
		isAuth := auth.IsAuthenticated(a.Sessions, r)
		var ownerClause string
		var ownerArg any
		if isAuth {
			ownerClause = "user_id = $2"
			ownerArg = auth.UserID(a.Sessions, r)
		} else {
			ownerClause = "session_id = $2"
			ownerArg = auth.SessionID(r)
		}

		var status, assetPNG, assetGLB, assetSTL string
		err := a.DB.QueryRow(r.Context(),
			`SELECT status, COALESCE(asset_png_path,''), COALESCE(asset_glb_path,''), COALESCE(asset_stl_path,'')
			 FROM generations WHERE id = $1 AND `+ownerClause,
			id, ownerArg,
		).Scan(&status, &assetPNG, &assetGLB, &assetSTL)
		if err != nil {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{
			"status":          status,
			"asset_png_path":  assetPNG,
			"asset_glb_path":  assetGLB,
			"asset_stl_path":  assetSTL,
		})
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// guestSessionID returns a stable per-guest key backed by the SCS session.
// It writes a marker into the session (minting a session token if one doesn't
// exist yet) and returns that token, so the same browser keeps the same key
// across requests and can't bypass the guest cooldown.
func guestSessionID(a *app.App, r *http.Request) string {
	if !a.Sessions.Exists(r.Context(), "guest") {
		a.Sessions.Put(r.Context(), "guest", true)
	}
	return a.Sessions.Token(r.Context())
}

// validSelection reports whether materialID and cutFinishID are real,
// configured options for the given product type.
func validSelection(a *app.App, productType, materialID, cutFinishID string) bool {
	switch productType {
	case "sticker":
		if _, ok := a.Config.Materials.StickerMaterialByID(materialID); !ok {
			return false
		}
		_, ok := a.Config.Materials.CutTypeByID(cutFinishID)
		return ok
	case "print":
		if _, ok := a.Config.Materials.PrintMaterialByID(materialID); !ok {
			return false
		}
		_, ok := a.Config.Materials.PrintFinishByID(cutFinishID)
		return ok
	default:
		return false
	}
}

// renderGenerateForm renders the /generate page (optionally with an error).
func renderGenerateForm(w http.ResponseWriter, r *http.Request, a *app.App, errMsg string) {
	d := pages.GeneratePageData{
		IsAuthenticated: auth.IsAuthenticated(a.Sessions, r),
		Error:           errMsg,
	}
	for _, m := range a.Config.Materials.Sticker.Materials {
		d.StickerMaterials = append(d.StickerMaterials, pages.GenerateOption{ID: m.ID, Name: m.DisplayName})
	}
	for _, c := range a.Config.Materials.Sticker.CutTypes {
		d.StickerCutTypes = append(d.StickerCutTypes, pages.GenerateOption{ID: c.ID, Name: c.DisplayName})
	}
	for _, m := range a.Config.Materials.Print.Materials {
		d.PrintMaterials = append(d.PrintMaterials, pages.GenerateOption{ID: m.ID, Name: m.DisplayName})
	}
	for _, f := range a.Config.Materials.Print.Finishes {
		d.PrintFinishes = append(d.PrintFinishes, pages.GenerateOption{ID: f.ID, Name: f.DisplayName})
	}
	if err := pages.GeneratePage(d).Render(r.Context(), w); err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
	}
}

func respondError(w http.ResponseWriter, r *http.Request, msg string, status int) {
	// HTMX-aware: return a partial error if HX-Request header present
	if r.Header.Get("HX-Request") == "true" {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(status)
		fmt.Fprintf(w, `<p class="text-[var(--color-danger-fg)] text-sm mt-2">%s</p>`, msg)
		return
	}
	http.Error(w, msg, status)
}

func respondCooldown(w http.ResponseWriter, r *http.Request, remaining time.Duration, isAuth bool) {
	secs := int(remaining.Seconds()) + 1
	nudge := ""
	if !isAuth {
		nudge = ` Account holders wait much less — <a href="/account/sign-up" class="underline">sign up free</a>.`
	}
	msg := fmt.Sprintf(`Give it a moment — try again in <strong>%ds</strong>.%s`, secs, nudge)

	if r.Header.Get("HX-Request") == "true" {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusTooManyRequests)
		fmt.Fprintf(w, `<div class="cooldown-notice">%s</div>`, msg)
		return
	}
	http.Error(w, "cooldown", http.StatusTooManyRequests)
}

// ── View data types (used by templ templates) ─────────────────────────────────

type recentGenEntry struct {
	ID           string
	Prompt       string
	ProductType  string
	AssetPNGPath pgtype.Text
	AssetGLBPath pgtype.Text
	Status       string
}

