// Package server wires HTTP routes and middleware to handler packages.
// It imports both internal/app (state) and all handler packages (handlers),
// breaking the import cycle that would occur if routing lived in internal/app.
package server

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	chimiddleware "github.com/go-chi/chi/v5/middleware"

	"github.com/wisward/uhhcraft/internal/account"
	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/auth"
	"github.com/wisward/uhhcraft/internal/canvas"
	"github.com/wisward/uhhcraft/internal/cart"
	"github.com/wisward/uhhcraft/internal/catalog"
	"github.com/wisward/uhhcraft/internal/checkout"
	"github.com/wisward/uhhcraft/internal/generation"
	"github.com/wisward/uhhcraft/internal/orders"
)

// Routes builds and returns the root HTTP handler.
func Routes(a *app.App) http.Handler {
	r := chi.NewRouter()

	// ── Global middleware ────────────────────────────────────────────────────
	r.Use(chimiddleware.RealIP)
	r.Use(chimiddleware.RequestID)
	r.Use(structuredLogger(a.Logger))
	r.Use(chimiddleware.Recoverer)
	r.Use(a.Sessions.LoadAndSave)
	r.Use(securityHeaders)

	// ── Static files (Caddy serves /static/* in production) ──────────────────
	r.Handle("/static/*", http.StripPrefix("/static/", http.FileServer(http.Dir("./web/static"))))

	// ── Health check ─────────────────────────────────────────────────────────
	// Path is /healthz to match compose.yml HEALTHCHECK, deploy.sh,
	// post-deploy.sh, and the agent-cloud production inventory health_path.
	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	// ── Public routes ─────────────────────────────────────────────────────────
	r.Get("/", catalog.HomeHandler(a))
	r.Get("/about", staticHandler("about"))

	// Catalog
	r.Get("/catalog", catalog.BrowseHandler(a))
	r.Get("/catalog/{category}", catalog.CategoryHandler(a))
	r.Get("/catalog/{category}/{slug}", catalog.ItemHandler(a))

	// Generation
	r.Get("/generate", generation.FormHandler(a))
	r.Post("/generate", generation.SubmitHandler(a))
	r.Get("/generate/status/{id}", generation.StatusHandler(a))

	// 3D Canvas
	r.Get("/canvas/{id}", canvas.ViewHandler(a))

	// Cart
	r.Get("/cart", cart.ViewHandler(a))
	r.Post("/cart/add", cart.AddHandler(a))
	r.Post("/cart/remove/{id}", cart.RemoveHandler(a))
	r.Post("/cart/update/{id}", cart.UpdateHandler(a))

	// Checkout
	r.Get("/checkout", checkout.StepHandler(a))
	r.Post("/checkout", checkout.StepPostHandler(a))
	r.Post("/checkout/payment-intent", checkout.CreatePaymentIntentHandler(a))

	// Order confirmation
	r.Get("/order/{id}", orders.ConfirmationHandler(a))

	// Auth
	r.Get("/account/sign-in", auth.SignInHandler(a))
	r.Post("/account/sign-in", auth.SignInPostHandler(a))
	r.Get("/account/sign-up", auth.SignUpHandler(a))
	r.Post("/account/sign-up", auth.SignUpPostHandler(a))
	r.Post("/account/sign-out", auth.SignOutHandler(a))
	r.Get("/account/forgot-password", auth.ForgotPasswordHandler(a))
	r.Post("/account/forgot-password", auth.ForgotPasswordPostHandler(a))
	r.Get("/account/reset-password", auth.ResetPasswordHandler(a))
	r.Post("/account/reset-password", auth.ResetPasswordPostHandler(a))
	r.Get("/account/verify-email", auth.VerifyEmailHandler(a))

	// ── Authenticated routes ──────────────────────────────────────────────────
	r.Group(func(r chi.Router) {
		r.Use(requireAuth(a))

		r.Get("/account", account.DashboardHandler(a))
		r.Get("/account/orders", account.OrdersHandler(a))
		r.Get("/account/orders/{id}", account.OrderDetailHandler(a))
		r.Get("/account/designs", account.DesignsHandler(a))
		r.Delete("/account/designs/{id}", account.DeleteDesignHandler(a))
	})

	// ── Webhook routes ────────────────────────────────────────────────────────
	r.Post("/webhooks/stripe", checkout.StripeWebhookHandler(a))
	r.Post("/webhooks/printify", orders.WebhookHandler(a, "printify"))
	r.Post("/webhooks/hubs", orders.WebhookHandler(a, "hubs"))

	// ── Legal / static pages ──────────────────────────────────────────────────
	r.Get("/legal/terms", staticHandler("legal-terms"))
	r.Get("/legal/privacy", staticHandler("legal-privacy"))
	r.Get("/legal/returns", staticHandler("legal-returns"))
	r.Get("/legal/accessibility", staticHandler("legal-accessibility"))

	// ── 404 / 500 ─────────────────────────────────────────────────────────────
	r.NotFound(func(w http.ResponseWriter, r *http.Request) {
		render404(w)
	})
	r.MethodNotAllowed(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusMethodNotAllowed)
	})

	return r
}

func staticHandler(page string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		renderStaticPage(w, page)
	}
}
