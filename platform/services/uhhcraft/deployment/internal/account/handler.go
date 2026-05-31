// Package account serves authenticated account management pages.
package account

import (
	"net/http"

	"github.com/go-chi/chi/v5"

	"github.com/wisward/uhhcraft/internal/app"
)

// DashboardHandler renders /account.
func DashboardHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "account dashboard — coming soon", http.StatusNotImplemented)
	}
}

// OrdersHandler renders /account/orders.
func OrdersHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "account orders — coming soon", http.StatusNotImplemented)
	}
}

// OrderDetailHandler renders /account/orders/{id}.
func OrderDetailHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_ = chi.URLParam(r, "id")
		http.Error(w, "account order detail — coming soon", http.StatusNotImplemented)
	}
}

// DesignsHandler renders /account/designs.
func DesignsHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "account designs — coming soon", http.StatusNotImplemented)
	}
}

// DeleteDesignHandler handles DELETE /account/designs/{id}.
func DeleteDesignHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_ = chi.URLParam(r, "id")
		http.Error(w, "delete design — coming soon", http.StatusNotImplemented)
	}
}
