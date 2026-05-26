package config

import (
	"fmt"
	"os"
	"strconv"

	"github.com/BurntSushi/toml"
)

// Config is the full runtime configuration for UhhCraft.
// Secrets come from environment variables; non-secret values come from TOML files.
type Config struct {
	App         AppConfig
	DB          DBConfig
	Redis       RedisConfig
	Storage     StorageConfig
	Stripe      StripeConfig
	Email       EmailConfig
	Discord     DiscordConfig
	Printify    PrintifyConfig
	Hubs        HubsConfig
	USPS        USPSConfig
	AI          AIConfig
	Sentry      SentryConfig
	Materials   MaterialsConfig
	Fulfillment FulfillmentConfig
}

type AppConfig struct {
	Env     string // development | production | test
	Port    int
	BaseURL string
	Secret  []byte // session secret, 32 bytes
}

type DBConfig struct {
	URL string
}

type RedisConfig struct {
	URL string
}

type StorageConfig struct {
	Endpoint  string
	AccessKey string
	SecretKey string
	Bucket    string
	UseSSL    bool
}

type StripeConfig struct {
	SecretKey      string
	PublishableKey string
	WebhookSecret  string
}

type EmailConfig struct {
	APIKey   string
	From     string
	FromName string
}

type DiscordConfig struct {
	OrdersWebhookURL string
	OpsWebhookURL    string
}

type PrintifyConfig struct {
	APIKey string
	ShopID string
}

type HubsConfig struct {
	APIKey string
}

type USPSConfig struct {
	ClientID     string
	ClientSecret string
}

type AIConfig struct {
	ImageServiceURL  string
	ThreeDServiceURL string
}

type SentryConfig struct {
	DSN string
}

// ── TOML-backed configs ───────────────────────────────────────────────────────

// MaterialsConfig mirrors config/materials.toml
type MaterialsConfig struct {
	Sticker StickerMaterials `toml:"sticker"`
	Print   PrintMaterials   `toml:"print"`
}

type StickerMaterials struct {
	Materials []Material `toml:"materials"`
	CutTypes  []CutType  `toml:"cut_types"`
}

type PrintMaterials struct {
	Materials []Material `toml:"materials"`
	Finishes  []Finish   `toml:"finishes"`
	SizeTiers []SizeTier `toml:"size_tiers"`
}

type Material struct {
	ID                  string  `toml:"id"`
	DisplayName         string  `toml:"display_name"`
	DescriptionCustomer string  `toml:"description_customer"`
	DescriptionOperator string  `toml:"description_operator"`
	PriceModifierUSD    float64 `toml:"price_modifier_usd"`
	Available           bool    `toml:"available"`
	SortOrder           int     `toml:"sort_order"`
}

type CutType struct {
	ID                  string  `toml:"id"`
	DisplayName         string  `toml:"display_name"`
	DescriptionCustomer string  `toml:"description_customer"`
	DescriptionOperator string  `toml:"description_operator"`
	PriceModifierUSD    float64 `toml:"price_modifier_usd"`
	Available           bool    `toml:"available"`
	SortOrder           int     `toml:"sort_order"`
}

type Finish = CutType // same shape

type SizeTier struct {
	ID           string  `toml:"id"`
	DisplayName  string  `toml:"display_name"`
	Description  string  `toml:"description"`
	BasePriceUSD float64 `toml:"base_price_usd"`
	SortOrder    int     `toml:"sort_order"`
}

// FulfillmentConfig mirrors relevant parts of config/printify.toml and config/fulfillment_3d.toml
type FulfillmentConfig struct {
	Printify PrintifyFulfillmentConfig `toml:"printify"`
	Hubs     HubsFulfillmentConfig     `toml:"hubs"`
}

type PrintifyFulfillmentConfig struct {
	APIBaseURL               string            `toml:"api_base_url"`
	RequestTimeoutSeconds    int               `toml:"request_timeout_seconds"`
	WebhookPath              string            `toml:"webhook_path"`
	MaterialBlueprintMap     map[string]string `toml:"material_blueprint_map"`
	CutTypeBlueprintOverride map[string]string `toml:"cut_type_blueprint_override"`
}

type HubsFulfillmentConfig struct {
	Enabled               bool              `toml:"enabled"`
	APIBaseURL            string            `toml:"api_base_url"`
	RequestTimeoutSeconds int               `toml:"request_timeout_seconds"`
	WebhookPath           string            `toml:"webhook_path"`
	MaterialMap           map[string]string `toml:"material_map"`
}

// ── Loader ────────────────────────────────────────────────────────────────────

// Load reads environment variables (secrets) and TOML files (non-secrets).
// configDir is the path to the config/ directory (e.g., "./config").
func Load(configDir string) (*Config, error) {
	cfg := &Config{}

	// App
	cfg.App.Env = getEnv("APP_ENV", "development")
	port, err := strconv.Atoi(getEnv("APP_PORT", "3000"))
	if err != nil {
		return nil, fmt.Errorf("APP_PORT: %w", err)
	}
	cfg.App.Port = port
	cfg.App.BaseURL = requireEnv("APP_BASE_URL")

	secret := requireEnv("SESSION_SECRET")
	if len(secret) < 32 {
		return nil, fmt.Errorf("SESSION_SECRET must be at least 32 characters")
	}
	cfg.App.Secret = []byte(secret)

	// Database & infra
	cfg.DB.URL = requireEnv("DATABASE_URL")
	cfg.Redis.URL = requireEnv("REDIS_URL")

	// Storage
	cfg.Storage.Endpoint = requireEnv("MINIO_ENDPOINT")
	cfg.Storage.AccessKey = requireEnv("MINIO_ACCESS_KEY")
	cfg.Storage.SecretKey = requireEnv("MINIO_SECRET_KEY")
	cfg.Storage.Bucket = getEnv("MINIO_BUCKET", "uhhcraft")
	cfg.Storage.UseSSL = getEnv("MINIO_USE_SSL", "false") == "true"

	// Stripe
	cfg.Stripe.SecretKey = requireEnv("STRIPE_SECRET_KEY")
	cfg.Stripe.PublishableKey = requireEnv("STRIPE_PUBLISHABLE_KEY")
	cfg.Stripe.WebhookSecret = requireEnv("STRIPE_WEBHOOK_SECRET")

	// Email
	cfg.Email.APIKey = requireEnv("RESEND_API_KEY")
	cfg.Email.From = getEnv("EMAIL_FROM", "orders@uhhcraft.uhstray.io")
	cfg.Email.FromName = getEnv("EMAIL_FROM_NAME", "UhhCraft")

	// Discord
	cfg.Discord.OrdersWebhookURL = requireEnv("DISCORD_ORDERS_WEBHOOK_URL")
	cfg.Discord.OpsWebhookURL = requireEnv("DISCORD_OPS_WEBHOOK_URL")

	// Third-party fulfillment
	cfg.Printify.APIKey = os.Getenv("PRINTIFY_API_KEY")
	cfg.Printify.ShopID = os.Getenv("PRINTIFY_SHOP_ID")
	cfg.Hubs.APIKey = os.Getenv("HUBS_API_KEY")

	// USPS
	cfg.USPS.ClientID = os.Getenv("USPS_CLIENT_ID")
	cfg.USPS.ClientSecret = os.Getenv("USPS_CLIENT_SECRET")

	// AI services — required from the environment. A hardcoded localhost
	// fallback would mask broken .env templating in production and silently
	// point the workers at nothing.
	cfg.AI.ImageServiceURL = requireEnv("AI_IMAGE_SERVICE_URL")
	cfg.AI.ThreeDServiceURL = requireEnv("AI_3D_SERVICE_URL")

	// Sentry (optional)
	cfg.Sentry.DSN = os.Getenv("SENTRY_DSN")

	// TOML: materials
	materialsPath := configDir + "/materials.toml"
	if _, err := toml.DecodeFile(materialsPath, &cfg.Materials); err != nil {
		return nil, fmt.Errorf("loading materials config: %w", err)
	}

	// TOML: fulfillment (partial — we only need non-secret fields)
	if err := loadFulfillmentConfig(configDir, cfg); err != nil {
		return nil, fmt.Errorf("loading fulfillment config: %w", err)
	}

	return cfg, nil
}

func loadFulfillmentConfig(configDir string, cfg *Config) error {
	var printifyCfg struct {
		Printify PrintifyFulfillmentConfig `toml:"printify"`
	}
	if _, err := toml.DecodeFile(configDir+"/printify.toml", &printifyCfg); err != nil {
		return fmt.Errorf("printify.toml: %w", err)
	}
	cfg.Fulfillment.Printify = printifyCfg.Printify

	var hubsCfg struct {
		Hubs HubsFulfillmentConfig `toml:"hubs"`
	}
	if _, err := toml.DecodeFile(configDir+"/fulfillment_3d.toml", &hubsCfg); err != nil {
		return fmt.Errorf("fulfillment_3d.toml: %w", err)
	}
	cfg.Fulfillment.Hubs = hubsCfg.Hubs

	return nil
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func requireEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		panic(fmt.Sprintf("required environment variable %s is not set", key))
	}
	return v
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// MaterialByID returns a sticker material or print material by ID.
// Returns (Material, true) if found, (Material{}, false) otherwise.
func (c *MaterialsConfig) StickerMaterialByID(id string) (Material, bool) {
	for _, m := range c.Sticker.Materials {
		if m.ID == id && m.Available {
			return m, true
		}
	}
	return Material{}, false
}

func (c *MaterialsConfig) CutTypeByID(id string) (CutType, bool) {
	for _, ct := range c.Sticker.CutTypes {
		if ct.ID == id && ct.Available {
			return ct, true
		}
	}
	return CutType{}, false
}

func (c *MaterialsConfig) PrintMaterialByID(id string) (Material, bool) {
	for _, m := range c.Print.Materials {
		if m.ID == id && m.Available {
			return m, true
		}
	}
	return Material{}, false
}

func (c *MaterialsConfig) PrintFinishByID(id string) (Finish, bool) {
	for _, f := range c.Print.Finishes {
		if f.ID == id && f.Available {
			return f, true
		}
	}
	return Finish{}, false
}
