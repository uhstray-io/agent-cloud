package server

import (
	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/generation"
)

// RegisterWorkers adds AI generation workers to the River pool.
// Called after app.New() but before River.Start().
func RegisterWorkers(a *app.App) {
	generation.RegisterWorkers(
		a.RiverWorkers,
		a.DB,
		a.Config.AI.ImageServiceURL,
		a.Config.AI.ThreeDServiceURL,
	)
}
