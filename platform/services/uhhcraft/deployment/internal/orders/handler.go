// Package orders serves order confirmation pages and fulfillment webhooks.
package orders

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/a-h/templ"
	"github.com/go-chi/chi/v5"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/auth"
	"github.com/wisward/uhhcraft/internal/db"
	"github.com/wisward/uhhcraft/web/templates/pages"
)

// ConfirmationHandler renders /order/{id}.
func ConfirmationHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		orderID := chi.URLParam(r, "id")

		// Fetch order. Money is read as integer cents (rounded from the
		// NUMERIC columns) to match the page's integer-cent contract.
		var o struct {
			ID            string
			GuestEmail    *string
			Status        string
			SubtotalCents int64
			ShippingCents int64
			DiscountCents int64
			TotalCents    int64
			ShipAddr      []byte
			Priority      bool
			UserID        *string
		}
		err := a.DB.QueryRow(r.Context(), `
			SELECT id::text, guest_email, status,
			       ROUND(subtotal_usd*100)::bigint, ROUND(shipping_usd*100)::bigint,
			       ROUND(discount_usd*100)::bigint, ROUND(total_usd*100)::bigint,
			       shipping_address, priority, user_id::text
			FROM orders WHERE id = $1`, orderID,
		).Scan(&o.ID, &o.GuestEmail, &o.Status,
			&o.SubtotalCents, &o.ShippingCents, &o.DiscountCents, &o.TotalCents,
			&o.ShipAddr, &o.Priority, &o.UserID)
		if err != nil {
			http.NotFound(w, r)
			return
		}

		// Ownership: an order tied to an account is visible only to that
		// account. Guest orders are reachable by ID but must not expose PII
		// (email / shipping address) to a bearer-of-the-link — those details
		// are delivered in the confirmation email instead. This prevents the
		// "public-by-ID confirmation leaks shipping data" problem.
		isAuth := auth.IsAuthenticated(a.Sessions, r)
		isOwner := false
		if o.UserID != nil {
			if !isAuth || auth.UserID(a.Sessions, r) != *o.UserID {
				http.NotFound(w, r)
				return
			}
			isOwner = true
		}

		// Parse shipping address
		var addrMap map[string]string
		_ = json.Unmarshal(o.ShipAddr, &addrMap)

		// Fetch order items
		rows, err := a.DB.Query(r.Context(), `
			SELECT oi.name, oi.material_id, oi.cut_finish_id,
			       COALESCE(cat.product_type, g.product_type, 'sticker'),
			       oi.quantity::int, ROUND(oi.unit_price_usd*100)::bigint
			FROM order_items oi
			LEFT JOIN catalog_items cat ON cat.id = oi.catalog_item_id
			LEFT JOIN generations g ON g.id = oi.generation_id
			WHERE oi.order_id = $1
			ORDER BY oi.created_at`, orderID)

		var items []pages.OrderLineItem
		if err == nil {
			defer rows.Close()
			for rows.Next() {
				var it pages.OrderLineItem
				var matID, cutID string
				if scanErr := rows.Scan(&it.Name, &matID, &cutID, &it.ProductType,
					&it.Quantity, &it.UnitPriceCents); scanErr == nil {
					it.LineTotalCents = it.UnitPriceCents * int64(it.Quantity)
					it.MaterialName = resolveMaterialName(a, matID, it.ProductType)
					it.CutFinishName = resolveCutFinishName(a, cutID, it.ProductType)
					items = append(items, it)
				}
			}
		}

		// Short ID (first 8 chars of UUID without hyphens)
		shortID := orderID
		if len(orderID) >= 8 {
			shortID = orderID[:8]
		}

		// PII (email + shipping name/address) is only rendered to the
		// authenticated owner. Guest confirmations show order id, status,
		// items and totals only; the full details are in the email receipt.
		email := ""
		shipName := ""
		shipAddr := ""
		if isOwner {
			if o.GuestEmail != nil {
				email = *o.GuestEmail
			} else if o.UserID != nil {
				_ = a.DB.QueryRow(r.Context(),
					`SELECT email FROM users WHERE id = $1`, *o.UserID).Scan(&email)
			}

			shipName = strings.TrimSpace(fmt.Sprintf("%s %s",
				addrMap["first_name"], addrMap["last_name"]))

			parts := []string{}
			if v := addrMap["address1"]; v != "" {
				parts = append(parts, v)
			}
			if v := addrMap["address2"]; v != "" {
				parts = append(parts, v)
			}
			if city, state, zip := addrMap["city"], addrMap["state"], addrMap["zip"]; city != "" {
				parts = append(parts, fmt.Sprintf("%s, %s %s", city, state, zip))
			}
			shipAddr = strings.Join(parts, ", ")
		}

		render(w, r, pages.OrderConfirmPage(pages.OrderConfirmData{
			IsAuthenticated: isAuth,
			OrderID:         o.ID,
			ShortID:         shortID,
			Email:           email,
			Status:          o.Status,
			Items:           items,
			SubtotalCents:   o.SubtotalCents,
			ShippingFree:    o.ShippingCents == 0,
			ShippingCents:   o.ShippingCents,
			TotalCents:      o.TotalCents,
			ShippingName:    shipName,
			ShippingAddress: shipAddr,
		}))
	}
}

// WebhookHandler handles fulfillment provider webhooks (Printify, Hubs).
//
// Provider-specific parsing and order-status updates are not implemented yet.
// Returning 501 (instead of a bare 200) means the provider keeps the event in
// its retry queue rather than treating it as durably processed — so fulfillment
// status cannot silently drift once the real handler is wired in.
func WebhookHandler(a *app.App, provider string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		a.Logger.Warn("fulfillment webhook received but processing not implemented",
			"provider", provider)
		http.Error(w, "fulfillment webhook processing not implemented", http.StatusNotImplemented)
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

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

func render(w http.ResponseWriter, r *http.Request, c templ.Component) {
	if err := c.Render(r.Context(), w); err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
	}
}

// Ensure db package is used (UUIDToString called externally if needed)
var _ = db.UUIDToString
