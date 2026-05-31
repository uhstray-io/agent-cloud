// Package checkout serves the multi-step checkout flow and Stripe webhooks.
package checkout

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"

	"github.com/a-h/templ"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	stripego "github.com/stripe/stripe-go/v79"
	"github.com/stripe/stripe-go/v79/paymentintent"
	"github.com/stripe/stripe-go/v79/webhook"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/auth"
	"github.com/wisward/uhhcraft/internal/db"
	"github.com/wisward/uhhcraft/web/templates/pages"
)

const (
	stickerBasePrice = 9.99
	printBasePrice   = 22.99
	freeShipThresh   = 50.0
	flatShipRate     = 7.99
)

// cartRow is the full snapshot of a cart line needed to build an order_items row.
// It is captured at PaymentIntent creation and replayed at webhook time so that
// edits to the live cart between intent creation and webhook delivery cannot
// change what ships or what was charged.
type cartRow struct {
	ID            string  `json:"id"`
	ItemType      string  `json:"item_type"`
	Name          string  `json:"name"`
	ProductType   string  `json:"product_type"`
	MaterialID    string  `json:"material_id"`
	CutFinishID   string  `json:"cut_finish_id"`
	Quantity      int     `json:"quantity"`
	UnitPrice     float64 `json:"unit_price"`
	CatalogItemID *string `json:"catalog_item_id"`
	GenerationID  *string `json:"generation_id"`
	AssetPath     string  `json:"asset_path"`
}

// StepHandler renders /checkout — shows contact form or payment step based on session.
func StepHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Check if contact info already stored → show payment step
		if a.Sessions.GetString(r.Context(), "checkout_email") != "" {
			showPaymentStep(w, r, a)
			return
		}

		isAuth := auth.IsAuthenticated(a.Sessions, r)
		email := ""
		if isAuth {
			userID := auth.UserID(a.Sessions, r)
			var u struct{ Email string }
			_ = a.DB.QueryRow(r.Context(),
				`SELECT email FROM users WHERE id = $1`, userID).Scan(&u.Email)
			email = u.Email
		}

		items, subtotal, _, total, shippingFree, shipping := cartSummary(r, a)

		render(w, r, pages.CheckoutContactPage(pages.CheckoutContactData{
			IsAuthenticated: isAuth,
			CartItems:       items,
			Subtotal:        subtotal,
			ShippingFree:    shippingFree,
			ShippingAmount:  shipping,
			Total:           total,
			Email:           email,
		}))
	}
}

// StepPostHandler processes step POST — validates contact form, advances to payment.
func StepPostHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}

		email := strings.TrimSpace(strings.ToLower(r.FormValue("email")))
		firstName := strings.TrimSpace(r.FormValue("first_name"))
		lastName := strings.TrimSpace(r.FormValue("last_name"))
		address1 := strings.TrimSpace(r.FormValue("address1"))
		city := strings.TrimSpace(r.FormValue("city"))
		state := strings.TrimSpace(strings.ToUpper(r.FormValue("state")))
		zip := strings.TrimSpace(r.FormValue("zip"))

		isAuth := auth.IsAuthenticated(a.Sessions, r)

		items, subtotal, _, total, shippingFree, shipping := cartSummary(r, a)

		// Validate. ZIP accepts 5-digit or ZIP+4 (12345 or 12345-6789).
		errMsg := ""
		switch {
		case !strings.Contains(email, "@"):
			errMsg = "Please enter a valid email address."
		case firstName == "" || lastName == "":
			errMsg = "Please enter your first and last name."
		case address1 == "":
			errMsg = "Please enter your street address."
		case city == "":
			errMsg = "Please enter your city."
		case len(state) != 2:
			errMsg = "Please enter a valid 2-letter state code."
		case !validZIP(zip):
			errMsg = "Please enter a valid ZIP code (12345 or 12345-6789)."
		case !isAuth && r.FormValue("age_gate") != "on":
			errMsg = "You must confirm that you are 13 years of age or older."
		}
		if errMsg != "" {
			render(w, r, pages.CheckoutContactPage(pages.CheckoutContactData{
				IsAuthenticated: isAuth,
				CartItems:       items,
				Subtotal:        subtotal,
				ShippingFree:    shippingFree,
				ShippingAmount:  shipping,
				Total:           total,
				Email:           email,
				FirstName:       firstName,
				LastName:        lastName,
				Address1:        address1,
				Address2:        r.FormValue("address2"),
				City:            city,
				State:           state,
				ZIP:             zip,
				Error:           errMsg,
			}))
			return
		}

		// Store contact info in session
		a.Sessions.Put(r.Context(), "checkout_email", email)
		a.Sessions.Put(r.Context(), "checkout_first_name", firstName)
		a.Sessions.Put(r.Context(), "checkout_last_name", lastName)
		a.Sessions.Put(r.Context(), "checkout_address1", address1)
		a.Sessions.Put(r.Context(), "checkout_address2", r.FormValue("address2"))
		a.Sessions.Put(r.Context(), "checkout_city", city)
		a.Sessions.Put(r.Context(), "checkout_state", state)
		a.Sessions.Put(r.Context(), "checkout_zip", zip)

		http.Redirect(w, r, "/checkout", http.StatusSeeOther)
	}
}

// validZIP accepts US ZIP (12345) and ZIP+4 (12345-6789).
func validZIP(zip string) bool {
	if len(zip) == 5 {
		return isDigits(zip)
	}
	if len(zip) == 10 && zip[5] == '-' {
		return isDigits(zip[:5]) && isDigits(zip[6:])
	}
	return false
}

func isDigits(s string) bool {
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return len(s) > 0
}

// CreatePaymentIntentHandler creates a Stripe PaymentIntent and returns the client secret.
func CreatePaymentIntentHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_, _, amountCents, _, _, _ := cartSummary(r, a)
		if amountCents == 0 {
			http.Error(w, "empty cart", http.StatusBadRequest)
			return
		}

		// Gather metadata
		email := a.Sessions.GetString(r.Context(), "checkout_email")
		userID := auth.UserID(a.Sessions, r)
		sessionID := sessionToken(r)

		addrJSON, err := json.Marshal(shippingAddressFromSession(r, a))
		if err != nil {
			a.Logger.Error("marshal shipping address for payment intent metadata", "err", err)
			http.Error(w, "checkout error", http.StatusInternalServerError)
			return
		}

		params := &stripego.PaymentIntentParams{
			Amount:   stripego.Int64(amountCents),
			Currency: stripego.String("usd"),
			AutomaticPaymentMethods: &stripego.PaymentIntentAutomaticPaymentMethodsParams{
				Enabled: stripego.Bool(true),
			},
			Metadata: map[string]string{
				"user_id":          userID,
				"session_id":       sessionID,
				"email":            email,
				"shipping_address": string(addrJSON),
			},
		}

		pi, err := paymentintent.New(params)
		if err != nil {
			a.Logger.Error("stripe payment intent", "err", err)
			http.Error(w, "payment setup failed", http.StatusInternalServerError)
			return
		}

		// Snapshot the cart against this PaymentIntent so the order is built
		// from exactly what was priced, regardless of later cart edits.
		if err := snapshotCart(r.Context(), a, pi.ID, userID, sessionID); err != nil {
			a.Logger.Error("cart snapshot", "pi", pi.ID, "err", err)
			http.Error(w, "payment setup failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"client_secret": pi.ClientSecret,
		})
	}
}

// StripeWebhookHandler validates and processes Stripe webhook events.
func StripeWebhookHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		const maxBodyBytes = int64(65536)
		r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)

		payload, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "read body", http.StatusBadRequest)
			return
		}

		sig := r.Header.Get("Stripe-Signature")
		event, err := webhook.ConstructEvent(payload, sig, a.Config.Stripe.WebhookSecret)
		if err != nil {
			a.Logger.Error("stripe webhook signature invalid", "err", err)
			http.Error(w, "invalid signature", http.StatusBadRequest)
			return
		}

		switch event.Type {
		case "payment_intent.succeeded":
			var pi stripego.PaymentIntent
			if err := json.Unmarshal(event.Data.Raw, &pi); err != nil {
				http.Error(w, "parse event", http.StatusBadRequest)
				return
			}
			// Process synchronously and only ACK on durable success. On
			// failure return 5xx so Stripe retries delivery rather than
			// leaving an orphaned paid PaymentIntent. Order creation is
			// idempotent, so retries are safe.
			if err := handlePaymentSucceeded(r.Context(), a, &pi); err != nil {
				a.Logger.Error("order creation failed", "pi", pi.ID, "err", err)
				_ = a.Discord.OpsAlert(context.Background(),
					"Order creation failed",
					fmt.Sprintf("PI: %s — %v", pi.ID, err))
				http.Error(w, "order creation failed", http.StatusInternalServerError)
				return
			}
		}

		w.WriteHeader(http.StatusOK)
	}
}

// ── Order creation ────────────────────────────────────────────────────────────

func handlePaymentSucceeded(ctx context.Context, a *app.App, pi *stripego.PaymentIntent) error {
	meta := pi.Metadata
	email := meta["email"]
	userIDStr := meta["user_id"]
	sessionIDStr := meta["session_id"]

	var addrMap map[string]string
	_ = json.Unmarshal([]byte(meta["shipping_address"]), &addrMap)

	// Idempotency guard: if an order already exists for this PaymentIntent,
	// this is a redelivery — treat as success without creating a duplicate.
	var existingID pgtype.UUID
	err := a.DB.QueryRow(ctx,
		`SELECT id FROM orders WHERE stripe_payment_intent_id = $1`, pi.ID).Scan(&existingID)
	if err == nil {
		return nil // already processed
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("idempotency check: %w", err)
	}

	// Load the cart snapshot captured at PaymentIntent creation.
	cartItems, err := loadSnapshot(ctx, a, pi.ID)
	if err != nil {
		return fmt.Errorf("load snapshot: %w", err)
	}
	if len(cartItems) == 0 {
		return fmt.Errorf("no snapshot items for pi=%s", pi.ID)
	}

	// Compute totals from the snapshot.
	subtotal := 0.0
	for _, it := range cartItems {
		subtotal += it.UnitPrice * float64(it.Quantity)
	}
	shippingFree := subtotal >= freeShipThresh
	shippingAmt := 0.0
	if !shippingFree {
		shippingAmt = flatShipRate
	}

	// Loyalty discount
	discountPct := 0
	if userIDStr != "" {
		_ = a.DB.QueryRow(ctx,
			`SELECT next_order_discount_pct FROM users WHERE id = $1`, userIDStr,
		).Scan(&discountPct)
	}
	discountAmt := 0.0
	if discountPct > 0 {
		discountAmt = subtotal * float64(discountPct) / 100.0
	}
	total := subtotal + shippingAmt - discountAmt

	shippingJSON, _ := json.Marshal(addrMap)
	priority := userIDStr != ""

	var userUUID pgtype.UUID
	if userIDStr != "" {
		_ = userUUID.Scan(userIDStr)
	}
	var guestEmail *string
	if !priority {
		guestEmail = &email
	}

	// All writes in one transaction so a mid-sequence failure rolls back
	// rather than leaving a partial paid order.
	tx, err := a.DB.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var orderID pgtype.UUID
	err = tx.QueryRow(ctx, `
		INSERT INTO orders
			(user_id, guest_email, status, subtotal_usd, shipping_usd, tax_usd,
			 discount_usd, discount_pct, total_usd, shipping_address,
			 priority, stripe_payment_intent_id, fulfillment_route, age_gate_confirmed)
		VALUES ($1, $2, 'paid', $3, $4, 0, $5, $6, $7, $8, $9, $10, 'inhouse', TRUE)
		ON CONFLICT (stripe_payment_intent_id) DO NOTHING
		RETURNING id`,
		userUUID, guestEmail, subtotal, shippingAmt,
		discountAmt, discountPct, total, shippingJSON,
		priority, pi.ID,
	).Scan(&orderID)
	if errors.Is(err, pgx.ErrNoRows) {
		// A concurrent delivery won the insert — already processed.
		return nil
	}
	if err != nil {
		return fmt.Errorf("create order: %w", err)
	}

	for _, it := range cartItems {
		var catUUID, genUUID pgtype.UUID
		if it.CatalogItemID != nil {
			_ = catUUID.Scan(*it.CatalogItemID)
		}
		if it.GenerationID != nil {
			_ = genUUID.Scan(*it.GenerationID)
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO order_items
				(order_id, item_type, catalog_item_id, generation_id,
				 name, material_id, cut_finish_id, quantity, unit_price_usd,
				 manufacturing_asset_path, status)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending')`,
			orderID, it.ItemType, catUUID, genUUID,
			it.Name, it.MaterialID, it.CutFinishID,
			it.Quantity, it.UnitPrice, it.AssetPath,
		); err != nil {
			return fmt.Errorf("create order item: %w", err)
		}
	}

	// Clear the live cart and snapshot inside the same transaction.
	if userIDStr != "" {
		_, _ = tx.Exec(ctx, `DELETE FROM cart_items WHERE user_id = $1`, userUUID)
	} else if sessionIDStr != "" {
		_, _ = tx.Exec(ctx, `DELETE FROM cart_items WHERE session_id = $1`, sessionIDStr)
	}
	_, _ = tx.Exec(ctx, `DELETE FROM order_snapshots WHERE payment_intent_id = $1`, pi.ID)

	// Loyalty discount for next order.
	if userIDStr != "" {
		newDiscount := 0
		if total >= 100 {
			newDiscount = 10
		} else if total >= 30 {
			newDiscount = 5
		}
		if _, err := tx.Exec(ctx,
			`UPDATE users SET next_order_discount_pct = $2 WHERE id = $1`,
			userUUID, newDiscount); err != nil {
			return fmt.Errorf("update loyalty: %w", err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit: %w", err)
	}

	orderIDStr := db.UUIDToString(orderID)

	// Best-effort post-commit side effects (notifications must not roll back
	// a durably-created order).
	_ = a.Discord.OrderPlaced(ctx, orderIDStr, total, priority)
	_ = a.Email.SendOrderConfirmation(ctx, email, orderIDStr,
		itemSummary(cartItems), fmt.Sprintf("$%.2f", total),
		formatAddress(addrMap))

	return nil
}

// ── Cart snapshot helpers ───────────────────────────────────────────────────

// fetchCartRows reads the full cart line data needed to build order items.
func fetchCartRows(ctx context.Context, a *app.App, userID, sessionID string) ([]cartRow, error) {
	const cartQuery = `
		SELECT
			ci.id::text, ci.item_type,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.name,'Item')
			     ELSE COALESCE('Your '||g.product_type,'Custom') END,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.product_type,'sticker')
			     ELSE COALESCE(g.product_type,'sticker') END,
			ci.material_id, ci.cut_finish_id,
			ci.quantity::int, ci.unit_price_usd::float8,
			CASE WHEN ci.item_type = 'catalog' THEN ci.catalog_item_id::text
			     ELSE NULL END,
			CASE WHEN ci.item_type = 'generated' THEN ci.generation_id::text
			     ELSE NULL END,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.thumbnail_path,'')
			     ELSE COALESCE(g.asset_png_path, g.asset_stl_path,'') END
		FROM cart_items ci
		LEFT JOIN catalog_items cat ON cat.id = ci.catalog_item_id
		LEFT JOIN generations g ON g.id = ci.generation_id
		WHERE %s AND (ci.locked_until IS NULL OR ci.locked_until > NOW())`

	var rows pgx.Rows
	var err error
	switch {
	case userID != "":
		rows, err = a.DB.Query(ctx, fmt.Sprintf(cartQuery, "ci.user_id = $1"), userID)
	case sessionID != "":
		rows, err = a.DB.Query(ctx, fmt.Sprintf(cartQuery, "ci.session_id = $1"), sessionID)
	default:
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var items []cartRow
	for rows.Next() {
		var it cartRow
		if err := rows.Scan(
			&it.ID, &it.ItemType, &it.Name, &it.ProductType,
			&it.MaterialID, &it.CutFinishID,
			&it.Quantity, &it.UnitPrice,
			&it.CatalogItemID, &it.GenerationID, &it.AssetPath,
		); err != nil {
			return nil, err
		}
		items = append(items, it)
	}
	return items, rows.Err()
}

// snapshotCart captures the cart against a PaymentIntent id.
func snapshotCart(ctx context.Context, a *app.App, piID, userID, sessionID string) error {
	rows, err := fetchCartRows(ctx, a, userID, sessionID)
	if err != nil {
		return err
	}
	blob, err := json.Marshal(rows)
	if err != nil {
		return err
	}
	_, err = a.DB.Exec(ctx, `
		INSERT INTO order_snapshots (payment_intent_id, items)
		VALUES ($1, $2)
		ON CONFLICT (payment_intent_id) DO UPDATE SET items = EXCLUDED.items`,
		piID, blob)
	return err
}

// loadSnapshot reads the cart snapshot captured for a PaymentIntent.
func loadSnapshot(ctx context.Context, a *app.App, piID string) ([]cartRow, error) {
	var blob []byte
	err := a.DB.QueryRow(ctx,
		`SELECT items FROM order_snapshots WHERE payment_intent_id = $1`, piID).Scan(&blob)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var items []cartRow
	if err := json.Unmarshal(blob, &items); err != nil {
		return nil, err
	}
	return items, nil
}

// ── Cart summary helper ───────────────────────────────────────────────────────

func sessionToken(r *http.Request) string {
	if c, err := r.Cookie("scs.session.token"); err == nil {
		return c.Value
	}
	return ""
}

func shippingAddressFromSession(r *http.Request, a *app.App) map[string]string {
	return map[string]string{
		"first_name": a.Sessions.GetString(r.Context(), "checkout_first_name"),
		"last_name":  a.Sessions.GetString(r.Context(), "checkout_last_name"),
		"address1":   a.Sessions.GetString(r.Context(), "checkout_address1"),
		"address2":   a.Sessions.GetString(r.Context(), "checkout_address2"),
		"city":       a.Sessions.GetString(r.Context(), "checkout_city"),
		"state":      a.Sessions.GetString(r.Context(), "checkout_state"),
		"zip":        a.Sessions.GetString(r.Context(), "checkout_zip"),
	}
}

// cartSummary returns (items, subtotal, amountCents, total, shippingFree, shippingAmt).
func cartSummary(r *http.Request, a *app.App) ([]pages.CartLineItem, float64, int64, float64, bool, float64) {
	userID := auth.UserID(a.Sessions, r)
	sessionID := ""
	if userID == "" {
		sessionID = sessionToken(r)
	}

	query := `
		SELECT
			ci.id::text, ci.item_type,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.name,'Item')
			     ELSE COALESCE('Your '||g.product_type,'Custom') END,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.product_type,'sticker')
			     ELSE COALESCE(g.product_type,'sticker') END,
			ci.material_id, ci.cut_finish_id,
			CASE WHEN ci.item_type = 'catalog' THEN COALESCE(cat.thumbnail_path,'')
			     ELSE COALESCE(g.asset_png_path,'') END,
			ci.quantity::int, ci.unit_price_usd::float8
		FROM cart_items ci
		LEFT JOIN catalog_items cat ON cat.id = ci.catalog_item_id
		LEFT JOIN generations g ON g.id = ci.generation_id
		WHERE %s AND (ci.locked_until IS NULL OR ci.locked_until > NOW())
		ORDER BY ci.created_at`

	var rows pgx.Rows
	var err error

	if userID != "" {
		rows, err = a.DB.Query(r.Context(), fmt.Sprintf(query, "ci.user_id = $1"), userID)
	} else if sessionID != "" {
		rows, err = a.DB.Query(r.Context(), fmt.Sprintf(query, "ci.session_id = $1"), sessionID)
	}

	var items []pages.CartLineItem
	if err == nil && rows != nil {
		defer rows.Close()
		for rows.Next() {
			var it pages.CartLineItem
			var matID, cutID string
			if scanErr := rows.Scan(&it.ID, &it.ItemType, &it.Name, &it.ProductType,
				&matID, &cutID, &it.ThumbnailURL, &it.Quantity, &it.UnitPrice); scanErr == nil {
				it.LineTotal = it.UnitPrice * float64(it.Quantity)
				it.MaterialName = matID // simplified for checkout summary
				it.CutFinishName = cutID
				items = append(items, it)
			}
		}
	}

	subtotal := 0.0
	for _, it := range items {
		subtotal += it.LineTotal
	}
	shippingFree := subtotal >= freeShipThresh
	shipping := 0.0
	if !shippingFree && len(items) > 0 {
		shipping = flatShipRate
	}
	total := subtotal + shipping
	// Round to the nearest cent before casting; float64 cannot represent
	// values like 9.99 exactly, so a bare int64(total*100) can truncate low.
	amountCents := int64(math.Round(total * 100))

	return items, subtotal, amountCents, total, shippingFree, shipping
}

func showPaymentStep(w http.ResponseWriter, r *http.Request, a *app.App) {
	items, subtotal, amountCents, total, shippingFree, shipping := cartSummary(r, a)

	if amountCents == 0 {
		// Cart expired or empty — restart checkout
		a.Sessions.Remove(r.Context(), "checkout_email")
		http.Redirect(w, r, "/cart", http.StatusSeeOther)
		return
	}

	userID := auth.UserID(a.Sessions, r)
	sessionID := sessionToken(r)
	addrJSON, _ := json.Marshal(shippingAddressFromSession(r, a))

	// Create payment intent. Metadata must carry session_id + shipping_address
	// so the webhook can resolve a guest cart snapshot and address.
	params := &stripego.PaymentIntentParams{
		Amount:   stripego.Int64(amountCents),
		Currency: stripego.String("usd"),
		AutomaticPaymentMethods: &stripego.PaymentIntentAutomaticPaymentMethodsParams{
			Enabled: stripego.Bool(true),
		},
		Metadata: map[string]string{
			"user_id":          userID,
			"session_id":       sessionID,
			"email":            a.Sessions.GetString(r.Context(), "checkout_email"),
			"shipping_address": string(addrJSON),
		},
	}
	pi, err := paymentintent.New(params)
	if err != nil {
		a.Logger.Error("stripe payment intent", "err", err)
		http.Error(w, "payment setup failed", http.StatusInternalServerError)
		return
	}

	if err := snapshotCart(r.Context(), a, pi.ID, userID, sessionID); err != nil {
		a.Logger.Error("cart snapshot", "pi", pi.ID, "err", err)
		http.Error(w, "payment setup failed", http.StatusInternalServerError)
		return
	}

	render(w, r, pages.CheckoutPaymentPage(pages.CheckoutPaymentData{
		IsAuthenticated:      auth.IsAuthenticated(a.Sessions, r),
		CartItems:            items,
		Subtotal:             subtotal,
		ShippingFree:         shippingFree,
		ShippingAmount:       shipping,
		Total:                total,
		StripePublishableKey: a.Config.Stripe.PublishableKey,
		StripeClientSecret:   pi.ClientSecret,
		Email:                a.Sessions.GetString(r.Context(), "checkout_email"),
	}))
}

func itemSummary(items []cartRow) string {
	if len(items) == 0 {
		return "your items"
	}
	names := make([]string, 0, len(items))
	for _, it := range items {
		names = append(names, fmt.Sprintf("%d× %s", it.Quantity, it.Name))
	}
	return strings.Join(names, ", ")
}

func formatAddress(m map[string]string) string {
	parts := []string{}
	if v := m["address1"]; v != "" {
		parts = append(parts, v)
	}
	if v := m["address2"]; v != "" {
		parts = append(parts, v)
	}
	city := m["city"]
	state := m["state"]
	zip := m["zip"]
	if city != "" || state != "" || zip != "" {
		parts = append(parts, fmt.Sprintf("%s, %s %s", city, state, zip))
	}
	return strings.Join(parts, ", ")
}

func render(w http.ResponseWriter, r *http.Request, c templ.Component) {
	if err := c.Render(r.Context(), w); err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
	}
}
