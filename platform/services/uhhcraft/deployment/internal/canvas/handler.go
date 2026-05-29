// Package canvas serves the 3D canvas product view.
package canvas

import (
	"context"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/auth"
	"github.com/wisward/uhhcraft/internal/db"
	"github.com/wisward/uhhcraft/internal/storage"
	"github.com/wisward/uhhcraft/web/templates/pages"
)

// ViewHandler serves /canvas/{id}.
// The ID may refer to:
//   - A generation record UUID (custom-generated item)
//   - A catalog item slug prefixed with "catalog-" (pre-designed item)
func ViewHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := chi.URLParam(r, "id")

		var data canvasData
		data.IsAuthenticated = auth.IsAuthenticated(a.Sessions, r)
		data.IsAdmin = auth.IsAdmin(a.Sessions, r)
		data.MaterialParam = r.URL.Query().Get("material")
		data.CutFinishParam = r.URL.Query().Get("cut")

		if strings.HasPrefix(id, "catalog-") {
			// Catalog item flow
			slug := strings.TrimPrefix(id, "catalog-")
			item, err := loadCatalogItem(r.Context(), a.DB, slug)
			if err != nil {
				http.NotFound(w, r)
				return
			}
			data.Mode = "catalog"
			data.ItemName = item.Name
			data.ProductType = item.ProductType
			data.Price = computePrice(a, item.BasePriceUSD, data.MaterialParam, data.CutFinishParam, item.ProductType)
			data.AssetGLBPath = item.ModelGlbPath.String
			data.AssetPNGPath = item.ImagePngPath.String
			data.CatalogSlug = slug
			data.ItemID = db.UUIDToString(item.ID)

			// Resolve presigned URL for Three.js (short TTL for display only).
			// Catalog assets are stored in UhhCraft's own MinIO; a presign
			// failure means the asset can't be shown, so surface a 5xx instead
			// of silently rendering an empty viewer.
			assetKey := data.AssetGLBPath
			if assetKey == "" {
				assetKey = data.AssetPNGPath
			}
			if assetKey != "" {
				url, err := a.Storage.PresignGet(r.Context(), assetKey, storage.CanvasURLTTL)
				if err != nil {
					a.Logger.Error("presign catalog asset", "key", assetKey, "err", err)
					http.Error(w, "asset temporarily unavailable", http.StatusBadGateway)
					return
				}
				data.AssetURL = url
			}

			data.MaterialOptions = filteredMaterials(a, item.MaterialIds, item.ProductType)
			data.CutFinishOptions = filteredCutFinish(a, item.CutFinishIds, item.ProductType)

		} else {
			// Generated item flow
			gen, err := loadGeneration(r.Context(), a.DB, id)
			if err != nil {
				http.NotFound(w, r)
				return
			}
			data.Mode = "generated"
			data.GenerationID = id
			data.ProductType = gen.ProductType
			data.Status = gen.Status
			data.Prompt = gen.Prompt
			data.MaterialParam = gen.MaterialId
			data.CutFinishParam = gen.CutFinishId
			data.ItemName = "Your " + productTypeLabel(gen.ProductType)

			// Generated assets live in the inference sidecars' MinIO and are
			// served through central Caddy at /generated/img/* and
			// /generated/3d/*. The worker stores those public, Caddy-routed
			// paths directly, so render them as-is rather than re-presigning
			// (which would couple the browser to MinIO internals and break the
			// /generated/* contract).
			data.AssetGLBPath = gen.AssetGlbPath.String
			data.AssetPNGPath = gen.AssetPngPath.String
			if gen.AssetGlbPath.String != "" {
				data.AssetURL = gen.AssetGlbPath.String
			} else {
				data.AssetURL = gen.AssetPngPath.String
			}

			data.Price = computePriceFromIDs(a, data.MaterialParam, data.CutFinishParam, gen.ProductType)
		}

		// NoIndex: generated canvas URLs are not indexable
		data.NoIndex = data.Mode == "generated"

		renderCanvasView(w, r, a, data)
	}
}

// ── Types ─────────────────────────────────────────────────────────────────────

type canvasData struct {
	Mode            string // "catalog" | "generated"
	IsAuthenticated bool
	IsAdmin         bool
	NoIndex         bool

	// Item identity
	ItemID       string
	CatalogSlug  string
	GenerationID string
	ItemName     string
	ProductType  string // "sticker" | "print"
	Prompt       string

	// Asset URLs (presigned)
	AssetURL    string
	AssetGLBPath string
	AssetPNGPath string

	// Generation state
	Status string // "pending" | "processing" | "completed" | "failed"

	// Material / finish
	MaterialParam    string
	CutFinishParam   string
	MaterialOptions  []materialOption
	CutFinishOptions []materialOption
	Price            float64
}

type materialOption struct {
	ID                  string
	DisplayName         string
	DescriptionCustomer string
	PriceModifierUSD    float64
	Selected            bool
}

// ── DB helpers ────────────────────────────────────────────────────────────────

type catalogRow struct {
	ID           pgtype.UUID
	Name         string
	ProductType  string
	BasePriceUSD pgtype.Numeric
	ModelGlbPath pgtype.Text
	ImagePngPath pgtype.Text
	MaterialIds  []string
	CutFinishIds []string
}

func loadCatalogItem(ctx context.Context, pool *pgxpool.Pool, slug string) (catalogRow, error) {
	var row catalogRow
	err := pool.QueryRow(ctx, `
		SELECT id, name, product_type, base_price_usd,
		       model_glb_path, image_png_path, material_ids, cut_finish_ids
		FROM catalog_items
		WHERE slug = $1 AND active = TRUE`, slug,
	).Scan(&row.ID, &row.Name, &row.ProductType, &row.BasePriceUSD,
		&row.ModelGlbPath, &row.ImagePngPath, &row.MaterialIds, &row.CutFinishIds)
	return row, err
}

type generationRow struct {
	ProductType  string
	Status       string
	Prompt       string
	MaterialId   string
	CutFinishId  string
	AssetPngPath pgtype.Text
	AssetGlbPath pgtype.Text
	AssetStlPath pgtype.Text
}

func loadGeneration(ctx context.Context, pool *pgxpool.Pool, id string) (generationRow, error) {
	var row generationRow
	err := pool.QueryRow(ctx, `
		SELECT product_type, status, prompt, material_id, cut_finish_id,
		       asset_png_path, asset_glb_path, asset_stl_path
		FROM generations
		WHERE id = $1`, id,
	).Scan(&row.ProductType, &row.Status, &row.Prompt, &row.MaterialId, &row.CutFinishId,
		&row.AssetPngPath, &row.AssetGlbPath, &row.AssetStlPath)
	return row, err
}

func computePrice(a *app.App, base pgtype.Numeric, materialID, cutFinishID, productType string) float64 {
	baseFloat, _ := base.Float64Value()
	return computePriceFromIDs(a, materialID, cutFinishID, productType) + baseFloat.Float64
}

func computePriceFromIDs(a *app.App, materialID, cutFinishID, productType string) float64 {
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

func filteredMaterials(a *app.App, ids []string, productType string) []materialOption {
	var opts []materialOption
	idSet := make(map[string]bool, len(ids))
	for _, id := range ids {
		idSet[id] = true
	}
	if productType == "sticker" {
		for _, m := range a.Config.Materials.Sticker.Materials {
			if m.Available && (len(ids) == 0 || idSet[m.ID]) {
				opts = append(opts, materialOption{
					ID: m.ID, DisplayName: m.DisplayName,
					DescriptionCustomer: m.DescriptionCustomer,
					PriceModifierUSD:    m.PriceModifierUSD,
				})
			}
		}
	} else {
		for _, m := range a.Config.Materials.Print.Materials {
			if m.Available && (len(ids) == 0 || idSet[m.ID]) {
				opts = append(opts, materialOption{
					ID: m.ID, DisplayName: m.DisplayName,
					DescriptionCustomer: m.DescriptionCustomer,
					PriceModifierUSD:    m.PriceModifierUSD,
				})
			}
		}
	}
	return opts
}

func filteredCutFinish(a *app.App, ids []string, productType string) []materialOption {
	var opts []materialOption
	idSet := make(map[string]bool, len(ids))
	for _, id := range ids {
		idSet[id] = true
	}
	if productType == "sticker" {
		for _, ct := range a.Config.Materials.Sticker.CutTypes {
			if ct.Available && (len(ids) == 0 || idSet[ct.ID]) {
				opts = append(opts, materialOption{
					ID: ct.ID, DisplayName: ct.DisplayName,
					DescriptionCustomer: ct.DescriptionCustomer,
					PriceModifierUSD:    ct.PriceModifierUSD,
				})
			}
		}
	} else {
		for _, f := range a.Config.Materials.Print.Finishes {
			if f.Available && (len(ids) == 0 || idSet[f.ID]) {
				opts = append(opts, materialOption{
					ID: f.ID, DisplayName: f.DisplayName,
					DescriptionCustomer: f.DescriptionCustomer,
					PriceModifierUSD:    f.PriceModifierUSD,
				})
			}
		}
	}
	return opts
}

func productTypeLabel(t string) string {
	if t == "sticker" {
		return "sticker"
	}
	return "3D print"
}

// renderCanvasView maps the handler view model to the page data and renders it.
func renderCanvasView(w http.ResponseWriter, r *http.Request, a *app.App, d canvasData) {
	pd := pages.CanvasData{
		Mode:            d.Mode,
		IsAuthenticated: d.IsAuthenticated,
		IsAdmin:         d.IsAdmin,
		NoIndex:         d.NoIndex,
		ItemID:          d.ItemID,
		CatalogSlug:     d.CatalogSlug,
		GenerationID:    d.GenerationID,
		ItemName:        d.ItemName,
		ProductType:     d.ProductType,
		Prompt:          d.Prompt,
		AssetURL:        d.AssetURL,
		AssetGLBPath:    d.AssetGLBPath,
		AssetPNGPath:    d.AssetPNGPath,
		Status:          d.Status,
		MaterialParam:   d.MaterialParam,
		CutFinishParam:  d.CutFinishParam,
		Price:           d.Price,
	}
	for _, m := range d.MaterialOptions {
		pd.MaterialOptions = append(pd.MaterialOptions, pages.CanvasMaterialOption(m))
	}
	for _, c := range d.CutFinishOptions {
		pd.CutFinishOptions = append(pd.CutFinishOptions, pages.CanvasMaterialOption(c))
	}
	if err := pages.CanvasPage(pd).Render(r.Context(), w); err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
	}
}
