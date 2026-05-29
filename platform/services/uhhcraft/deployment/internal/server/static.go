package server

import (
	"fmt"
	"net/http"

	"github.com/wisward/uhhcraft/internal/app"
)

func renderStaticPage(w http.ResponseWriter, r *http.Request, page string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>UhhCraft</title>
<link rel="stylesheet" href="/static/css/app.css">
</head><body class="min-h-screen bg-[var(--color-bg-page)] text-[var(--color-fg-default)] font-sans p-8">
<a href="/" class="text-[var(--color-brand-solid)] font-semibold">← Home</a>
<h1 class="text-3xl font-bold mt-6 mb-4">%s</h1>
<p class="text-[var(--color-fg-muted)]">This page is coming soon.</p>
</body></html>`, page)
}

func render404(w http.ResponseWriter, r *http.Request, a *app.App) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusNotFound)
	fmt.Fprint(w, `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Not Found | UhhCraft</title>
<link rel="stylesheet" href="/static/css/app.css">
</head><body class="min-h-screen bg-[var(--color-bg-page)] text-[var(--color-fg-default)] font-sans flex flex-col items-center justify-center p-8 text-center">
<div class="text-7xl mb-6" aria-hidden="true">🦊</div>
<h1 class="text-4xl font-extrabold mb-2">404</h1>
<p class="text-[var(--color-fg-muted)] mb-6">We looked everywhere. That page doesn't exist.</p>
<a href="/" class="inline-flex items-center px-5 py-2.5 rounded-[var(--radius-md)] bg-[var(--color-brand-solid)] text-[var(--color-fg-default)] font-semibold">Go Home</a>
</body></html>`)
}
