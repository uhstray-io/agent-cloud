// Package cart serves the cart view and mutation endpoints.
package cart

import (
	"fmt"
	"net/http"
	"strconv"

	"github.com/a-h/templ"
	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/auth"
	"github.com/wisward/uhhcraft/web/templates/pages"
)

const (
	stickerBasePrice = 9.99
	printBasePrice   = 22.99
	freeShipThresh   = 50.0
	flatShipRate     = 7.99
)

// ViewHandler renders /cart.
func ViewHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		items, err := queryCartItems(r, a)
		if err != nil {
			a.Logger.Error("cart query", "err", err)
		}

		subtotal := 0.0
		count := 0
		for _, it := range items {
			subtotal += it.LineTotal
			count += it.Quantity
		}
		shippingFree := subtotal >= freeShipThresh
		shipping := 0.0
		if !shippingFree && len(items) > 0 {
			shipping = flatShipRate
		}

		render(w, r, pages.CartPage(pages.CartData{
			IsAuthenticated: auth.IsAuthenticated(a.Sessions, r),
			Items:           items,
			Subtotal:        subtotal,
			ShippingFree:    shippingFree,
			ShippingAmount:  shipping,
			Total:           subtotal + shipping,
			ItemCount:       count,
		}))
	}
}

// AddHandler handles POST /cart/add.
func AddHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}

		itemType := r.FormValue("item_type") // "catalog" or "generated"
		materialID := r.FormValue("material_id")
		cutFinishID := r.FormValue("cut_finish_id")

		if itemType != "catalog" && itemType != "generated" {
			http.Error(w, "invalid item type", http.StatusBadRequest)
			return
		}

		// Compute unit price
		var unitPrice float64
		var productType string

		if itemType == "catalog" {
			itemIDStr := r.FormValue("catalog_item_id")
			var basePrice float64
			var prodType string
			err := a.DB.QueryRow(r.Context(),
				`SELECT base_price_usd::float8, product_type FROM catalog_items WHERE id = $1 AND active = TRUE`,
				itemIDStr,
			).Scan(&basePrice, &prodType)
			if err != nil {
				http.Error(w, "item not found", http.StatusNotFound)
				return
			}
			productType = prodType
			unitPrice = basePrice + materialModifier(a, materialID, cutFinishID, prodType)
		}

		// Determine cart owner up front so generated-item access can be scoped.
		userID, sessionID := cartOwner(r, a)

		if itemType == "generated" {
			genIDStr := r.FormValue("generation_id")
			// Scope the generation to the requester: an account user may only
			// add their own generations; a guest only those tied to their
			// session. This stops a guessed generation UUID from being added
			// to someone else's cart.
			var ownerClause string
			var ownerArg any
			if userID != "" {
				ownerClause = "user_id = $2"
				ownerArg = userID
			} else {
				ownerClause = "session_id = $2"
				ownerArg = sessionID
			}
			var prodType, status string
			err := a.DB.QueryRow(r.Context(),
				`SELECT product_type, status FROM generations WHERE id = $1 AND `+ownerClause,
				genIDStr, ownerArg,
			).Scan(&prodType, &status)
			if err != nil || status != "completed" {
				http.Error(w, "generation not ready", http.StatusBadRequest)
				return
			}
			productType = prodType
			base := stickerBasePrice
			if prodType == "print" {
				base = printBasePrice
			}
			unitPrice = base + materialModifier(a, materialID, cutFinishID, prodType)
		}

		// Build the INSERT
		var catalogItemID pgtype.UUID
		var generationID pgtype.UUID
		if itemType == "catalog" {
			_ = catalogItemID.Scan(r.FormValue("catalog_item_id"))
		} else {
			_ = generationID.Scan(r.FormValue("generation_id"))
		}

		var userIDVal pgtype.UUID
		if userID != "" {
			_ = userIDVal.Scan(userID)
		}
		sessionVal := pgtype.Text{String: sessionID, Valid: sessionID != ""}

		_ = productType

		// Check if the same catalog item is already in the cart (same item +
		// material + cut) → increment instead of duplicate. Generated items are
		// unique and never merged.
		if itemType == "catalog" {
			var existingID pgtype.UUID
			err := a.DB.QueryRow(r.Context(),
				`SELECT id FROM cart_items
				 WHERE catalog_item_id = $1 AND material_id = $2 AND cut_finish_id = $3
				   AND (user_id = $4 OR session_id = $5)
				 LIMIT 1`,
				catalogItemID, materialID, cutFinishID, userIDVal, sessionVal,
			).Scan(&existingID)
			if err == nil {
				if _, err := a.DB.Exec(r.Context(),
					`UPDATE cart_items SET quantity = quantity + 1 WHERE id = $1`,
					existingID); err != nil {
					a.Logger.Error("cart increment", "err", err)
					http.Error(w, "server error", http.StatusInternalServerError)
					return
				}
				respondCartAdded(w, r)
				return
			}
		}

		if itemType == "generated" {
			// Atomically reserve the one-of-a-kind generation: the row is
			// inserted only if no other active cart row already holds it.
			// RowsAffected == 0 means another cart claimed it first.
			tag, err := a.DB.Exec(r.Context(),
				`INSERT INTO cart_items
					(user_id, session_id, item_type, catalog_item_id, generation_id,
					 material_id, cut_finish_id, quantity, unit_price_usd, locked_until)
				 SELECT $1, $2, $3, $4, $5, $6, $7, 1, $8, NOW() + INTERVAL '30 minutes'
				 WHERE NOT EXISTS (
					SELECT 1 FROM cart_items
					WHERE generation_id = $5
					  AND (locked_until IS NULL OR locked_until > NOW())
				 )`,
				userIDVal, sessionVal, itemType, catalogItemID, generationID,
				materialID, cutFinishID, unitPrice,
			)
			if err != nil {
				a.Logger.Error("add to cart (generated)", "err", err)
				http.Error(w, "server error", http.StatusInternalServerError)
				return
			}
			if tag.RowsAffected() == 0 {
				http.Error(w, "this one-of-a-kind item is already in a cart", http.StatusConflict)
				return
			}
			respondCartAdded(w, r)
			return
		}

		// Catalog item — plain insert (no reservation lock).
		if _, err := a.DB.Exec(r.Context(),
			`INSERT INTO cart_items
				(user_id, session_id, item_type, catalog_item_id, generation_id,
				 material_id, cut_finish_id, quantity, unit_price_usd, locked_until)
				VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, NULL)`,
			userIDVal, sessionVal, itemType, catalogItemID, generationID,
			materialID, cutFinishID, unitPrice,
		); err != nil {
			a.Logger.Error("add to cart", "err", err)
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}

		respondCartAdded(w, r)
	}
}

// RemoveHandler handles POST /cart/remove/{id}.
func RemoveHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := chi.URLParam(r, "id")
		userID, sessionID := cartOwner(r, a)

		var userIDVal pgtype.UUID
		if userID != "" {
			_ = userIDVal.Scan(userID)
		}
		sessionVal := pgtype.Text{String: sessionID, Valid: sessionID != ""}

		if _, err := a.DB.Exec(r.Context(),
			`DELETE FROM cart_items WHERE id = $1
			 AND (user_id = $2 OR session_id = $3)`,
			id, userIDVal, sessionVal); err != nil {
			a.Logger.Error("cart remove", "err", err)
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}

		// HTMX: return empty string to remove the element
		if r.Header.Get("HX-Request") == "true" {
			w.WriteHeader(http.StatusOK)
			return
		}
		http.Redirect(w, r, "/cart", http.StatusSeeOther)
	}
}

// UpdateHandler handles POST /cart/update/{id}.
func UpdateHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := chi.URLParam(r, "id")
		qty, _ := strconv.Atoi(r.FormValue("quantity"))
		if qty < 1 {
			qty = 1
		}
		if qty > 10 {
			qty = 10
		}

		userID, sessionID := cartOwner(r, a)
		var userIDVal pgtype.UUID
		if userID != "" {
			_ = userIDVal.Scan(userID)
		}
		sessionVal := pgtype.Text{String: sessionID, Valid: sessionID != ""}

		if _, err := a.DB.Exec(r.Context(),
			`UPDATE cart_items SET quantity = $2 WHERE id = $1
			 AND (user_id = $3 OR session_id = $4)`,
			id, qty, userIDVal, sessionVal); err != nil {
			a.Logger.Error("cart update", "err", err)
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}

		http.Redirect(w, r, "/cart", http.StatusSeeOther)
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func cartOwner(r *http.Request, a *app.App) (userID, sessionID string) {
	userID = auth.UserID(a.Sessions, r)
	if userID == "" {
		c, err := r.Cookie("scs.session.token")
		if err == nil {
			sessionID = c.Value
		}
	}
	return
}

func materialModifier(a *app.App, materialID, cutFinishID, productType string) float64 {
	total := 0.0
	if productType == "sticker" {
		if m, ok := a.Config.Materials.StickerMaterialByID(materialID); ok {
			total += m.PriceModifierUSD
		}
		if ct, ok := a.Config.Materials.CutTypeByID(cutFinishID); ok {
			total += ct.PriceModifierUSD
		}
	} else {
		if m, ok := a.Config.Materials.PrintMaterialByID(materialID); ok {
			total += m.PriceModifierUSD
		}
		if f, ok := a.Config.Materials.PrintFinishByID(cutFinishID); ok {
			total += f.PriceModifierUSD
		}
	}
	return total
}

func queryCartItems(r *http.Request, a *app.App) ([]pages.CartLineItem, error) {
	userID, sessionID := cartOwner(r, a)

	var rows interface {
		Next() bool
		Scan(...any) error
		Close()
	}
	var err error

	query := `
		SELECT
			ci.id::text,
			ci.item_type,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.name, 'Item')
			     ELSE COALESCE('Your ' || g.product_type, 'Custom') END,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.product_type, 'sticker')
			     ELSE COALESCE(g.product_type, 'sticker') END,
			ci.material_id,
			ci.cut_finish_id,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.thumbnail_path, '')
			     ELSE COALESCE(g.asset_png_path, '') END,
			ci.quantity::int,
			ci.unit_price_usd::float8
		FROM cart_items ci
		LEFT JOIN catalog_items cat ON cat.id = ci.catalog_item_id
		LEFT JOIN generations g ON g.id = ci.generation_id
		WHERE %s
		  AND (ci.locked_until IS NULL OR ci.locked_until > NOW())
		ORDER BY ci.created_at`

	if userID != "" {
		rows, err = a.DB.Query(r.Context(),
			fmt.Sprintf(query, "ci.user_id = $1"),
			userID)
	} else if sessionID != "" {
		rows, err = a.DB.Query(r.Context(),
			fmt.Sprintf(query, "ci.session_id = $1"),
			sessionID)
	} else {
		return nil, nil
	}

	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var items []pages.CartLineItem
	for rows.Next() {
		var it pages.CartLineItem
		var matID, cutID string
		if err := rows.Scan(&it.ID, &it.ItemType, &it.Name, &it.ProductType,
			&matID, &cutID, &it.ThumbnailURL, &it.Quantity, &it.UnitPrice); err != nil {
			continue
		}
		it.LineTotal = it.UnitPrice * float64(it.Quantity)
		it.MaterialName = resolveMaterialName(a, matID, it.ProductType)
		it.CutFinishName = resolveCutFinishName(a, cutID, it.ProductType)
		items = append(items, it)
	}
	return items, nil
}

func resolveMaterialName(a *app.App, matID, productType string) string {
	if productType == "sticker" {
		if m, ok := a.Config.Materials.StickerMaterialByID(matID); ok {
			return m.DisplayName
		}
	} else {
		if m, ok := a.Config.Materials.PrintMaterialByID(matID); ok {
			return m.DisplayName
		}
	}
	return matID
}

func resolveCutFinishName(a *app.App, id, productType string) string {
	if productType == "sticker" {
		if ct, ok := a.Config.Materials.CutTypeByID(id); ok {
			return ct.DisplayName
		}
	} else {
		if f, ok := a.Config.Materials.PrintFinishByID(id); ok {
			return f.DisplayName
		}
	}
	return ""
}

func respondCartAdded(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("HX-Request") == "true" {
		w.Header().Set("Content-Type", "text/html")
		fmt.Fprint(w, `<span class="text-xs text-[var(--color-success-fg)] font-medium">Added to cart ✓</span>`)
		return
	}
	http.Redirect(w, r, "/cart", http.StatusSeeOther)
}

func render(w http.ResponseWriter, r *http.Request, c templ.Component) {
	if err := c.Render(r.Context(), w); err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
	}
}
