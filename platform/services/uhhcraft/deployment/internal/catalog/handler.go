// Package catalog serves catalog browsing and the home page.
package catalog

import (
	"errors"
	"net/http"

	"github.com/a-h/templ"
	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/auth"
	"github.com/wisward/uhhcraft/web/templates/pages"
)

// HomeHandler renders the home page with showcase items.
func HomeHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		isAuth := auth.IsAuthenticated(a.Sessions, r)

		rows, err := a.DB.Query(r.Context(), `
			SELECT ci.slug, ci.name, ci.product_type, COALESCE(ci.thumbnail_path,''), COALESCE(si.caption,'')
			FROM showcase_items si
			JOIN catalog_items ci ON ci.id = si.catalog_item_id
			WHERE si.active = TRUE AND ci.active = TRUE
			ORDER BY si.sort_order ASC
			LIMIT 8
		`)

		if err != nil {
			a.Logger.Error("home showcase query", "err", err)
			http.Error(w, "service temporarily unavailable", http.StatusInternalServerError)
			return
		}
		var items []pages.HomeShowcaseItem
		defer rows.Close()
		for rows.Next() {
			var it pages.HomeShowcaseItem
			_ = rows.Scan(&it.Slug, &it.Name, &it.ProductType, &it.ThumbnailURL, &it.Caption)
			items = append(items, it)
		}

		render(w, r, pages.HomePage(pages.HomeData{
			IsAuthenticated: isAuth,
			ShowcaseItems:   items,
		}))
	}
}

// BrowseHandler renders the full catalog listing (/catalog).
func BrowseHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		isAuth := auth.IsAuthenticated(a.Sessions, r)

		// Query items
		rows, err := a.DB.Query(r.Context(), `
			SELECT slug, name, description, product_type, COALESCE(thumbnail_path,''), base_price_usd::float8
			FROM catalog_items
			WHERE active = TRUE
			ORDER BY created_at DESC
		`)

		if err != nil {
			a.Logger.Error("catalog browse query", "err", err)
			http.Error(w, "service temporarily unavailable", http.StatusInternalServerError)
			return
		}
		var items []pages.CatalogItem
		defer rows.Close()
		for rows.Next() {
			var it pages.CatalogItem
			_ = rows.Scan(&it.Slug, &it.Name, &it.Description, &it.ProductType, &it.ThumbnailURL, &it.BasePriceUSD)
			items = append(items, it)
		}

		// Query categories
		catRows, _ := a.DB.Query(r.Context(), `
			SELECT cc.slug, cc.name, COUNT(ci.id) AS count
			FROM catalog_categories cc
			LEFT JOIN catalog_items ci ON ci.category_id = cc.id AND ci.active = TRUE
			WHERE cc.active = TRUE
			GROUP BY cc.slug, cc.name, cc.sort_order
			ORDER BY cc.sort_order
		`)
		var cats []pages.CatalogCategory
		if catRows != nil {
			defer catRows.Close()
			for catRows.Next() {
				var c pages.CatalogCategory
				_ = catRows.Scan(&c.Slug, &c.Name, &c.Count)
				cats = append(cats, c)
			}
		}

		render(w, r, pages.CatalogBrowsePage(pages.CatalogBrowseData{
			IsAuthenticated: isAuth,
			Items:           items,
			Categories:      cats,
			TotalCount:      len(items),
		}))
	}
}

// CategoryHandler renders a category listing (/catalog/{category}).
func CategoryHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		slug := chi.URLParam(r, "category")
		isAuth := auth.IsAuthenticated(a.Sessions, r)

		rows, err := a.DB.Query(r.Context(), `
			SELECT ci.slug, ci.name, ci.description, ci.product_type, COALESCE(ci.thumbnail_path,''), ci.base_price_usd::float8
			FROM catalog_items ci
			JOIN catalog_categories cc ON cc.id = ci.category_id
			WHERE cc.slug = $1 AND ci.active = TRUE
			ORDER BY ci.created_at DESC
		`, slug)

		if err != nil {
			a.Logger.Error("catalog category query", "err", err)
			http.Error(w, "service temporarily unavailable", http.StatusInternalServerError)
			return
		}
		var items []pages.CatalogItem
		defer rows.Close()
		for rows.Next() {
			var it pages.CatalogItem
			_ = rows.Scan(&it.Slug, &it.Name, &it.Description, &it.ProductType, &it.ThumbnailURL, &it.BasePriceUSD)
			items = append(items, it)
		}

		// The active category's display name is resolved client-side from the
		// Categories list below (it carries slug + name), so no extra query here.
		catRows, _ := a.DB.Query(r.Context(), `
			SELECT cc.slug, cc.name, COUNT(ci.id) AS count
			FROM catalog_categories cc
			LEFT JOIN catalog_items ci ON ci.category_id = cc.id AND ci.active = TRUE
			WHERE cc.active = TRUE
			GROUP BY cc.slug, cc.name, cc.sort_order
			ORDER BY cc.sort_order
		`)
		var cats []pages.CatalogCategory
		if catRows != nil {
			defer catRows.Close()
			for catRows.Next() {
				var c pages.CatalogCategory
				_ = catRows.Scan(&c.Slug, &c.Name, &c.Count)
				cats = append(cats, c)
			}
		}

		render(w, r, pages.CatalogBrowsePage(pages.CatalogBrowseData{
			IsAuthenticated: isAuth,
			Items:           items,
			Categories:      cats,
			ActiveCategory:  slug,
			TotalCount:      len(items),
		}))
	}
}

// ItemHandler redirects to the canvas view for a catalog item.
func ItemHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		slug := chi.URLParam(r, "slug")
		isAuth := auth.IsAuthenticated(a.Sessions, r)

		var item pages.CatalogItem
		var catSlug, catName string
		err := a.DB.QueryRow(r.Context(), `
			SELECT ci.slug, ci.name, ci.description, ci.product_type, COALESCE(ci.thumbnail_path,''), ci.base_price_usd::float8,
			       COALESCE(cc.slug,''), COALESCE(cc.name,'')
			FROM catalog_items ci
			LEFT JOIN catalog_categories cc ON cc.id = ci.category_id
			WHERE ci.slug = $1 AND ci.active = TRUE
		`, slug).Scan(&item.Slug, &item.Name, &item.Description, &item.ProductType, &item.ThumbnailURL, &item.BasePriceUSD, &catSlug, &catName)

		if errors.Is(err, pgx.ErrNoRows) {
			http.NotFound(w, r)
			return
		}
		if err != nil {
			a.Logger.Error("catalog item query", "slug", slug, "err", err)
			http.Error(w, "service temporarily unavailable", http.StatusInternalServerError)
			return
		}

		render(w, r, pages.CatalogItemPage(pages.CatalogItemData{
			IsAuthenticated: isAuth,
			Item:            item,
			CategorySlug:    catSlug,
			CategoryName:    catName,
		}))
	}
}

func render(w http.ResponseWriter, r *http.Request, c templ.Component) {
	if err := c.Render(r.Context(), w); err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
	}
}
