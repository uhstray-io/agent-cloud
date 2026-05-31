// Package discord sends notifications to Discord via incoming webhooks.
package discord

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// Client sends messages to Discord webhook URLs.
type Client struct {
	ordersURL string
	opsURL    string
	http      *http.Client
}

// New creates a Discord client from webhook URLs.
func New(ordersURL, opsURL string) *Client {
	return &Client{
		ordersURL: ordersURL,
		opsURL:    opsURL,
		http:      &http.Client{Timeout: 10 * time.Second},
	}
}

type message struct {
	Content string  `json:"content,omitempty"`
	Embeds  []embed `json:"embeds,omitempty"`
}

type embed struct {
	Title       string  `json:"title,omitempty"`
	Description string  `json:"description,omitempty"`
	Color       int     `json:"color,omitempty"` // decimal colour
	Fields      []field `json:"fields,omitempty"`
	Timestamp   string  `json:"timestamp,omitempty"` // ISO 8601
}

type field struct {
	Name   string `json:"name"`
	Value  string `json:"value"`
	Inline bool   `json:"inline"`
}

// OrderPlaced sends a payment notification to the orders webhook.
// It deliberately does NOT include the customer's email address — Discord is
// not an appropriate destination for customer PII. The order ID is sufficient
// for staff to look the order up in the admin/DB.
func (c *Client) OrderPlaced(ctx context.Context, orderID string, totalUSD float64, priority bool) error {
	priorityText := ""
	if priority {
		priorityText = " ⭐ Priority"
	}
	return c.send(ctx, c.ordersURL, message{
		Embeds: []embed{{
			Title:       "💰 New Order" + priorityText,
			Color:       0xE8732A, // brand orange
			Description: fmt.Sprintf("Order `%s`", orderID),
			Fields: []field{
				{Name: "Total", Value: fmt.Sprintf("$%.2f", totalUSD), Inline: true},
			},
			Timestamp: time.Now().UTC().Format(time.RFC3339),
		}},
	})
}

// OrderRouted sends a fulfillment routing notification.
func (c *Client) OrderRouted(ctx context.Context, orderID, provider string) error {
	return c.send(ctx, c.ordersURL, message{
		Embeds: []embed{{
			Title:       "📦 Order Routed",
			Color:       0x5BBED6, // brand blue
			Description: fmt.Sprintf("Order `%s` → **%s**", orderID, provider),
			Timestamp:   time.Now().UTC().Format(time.RFC3339),
		}},
	})
}

// OpsAlert sends an ops/error alert to the ops webhook.
func (c *Client) OpsAlert(ctx context.Context, title, detail string) error {
	return c.send(ctx, c.opsURL, message{
		Embeds: []embed{{
			Title:       "⚠️ " + title,
			Color:       0xDC2626, // danger red
			Description: detail,
			Timestamp:   time.Now().UTC().Format(time.RFC3339),
		}},
	})
}

func (c *Client) send(ctx context.Context, url string, msg message) error {
	if url == "" {
		return nil // webhook not configured; silently skip
	}
	body, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("discord marshal: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("discord post: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return fmt.Errorf("discord returned %d", resp.StatusCode)
	}
	return nil
}
