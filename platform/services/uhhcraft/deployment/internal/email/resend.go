// Package email sends transactional emails via Resend.
package email

import (
	"context"
	"fmt"
	"html"
	"net/mail"
	"net/url"

	resend "github.com/resend/resend-go/v2"

	"github.com/wisward/uhhcraft/internal/config"
)

// shortID returns the first 8 chars of an order id for display, guarding
// against ids shorter than 8 (which would panic on a raw slice).
func shortID(id string) string {
	if len(id) >= 8 {
		return id[:8]
	}
	return id
}

// Client wraps the Resend API for transactional email.
type Client struct {
	resend   *resend.Client
	from     string
	fromName string
	baseURL  string
}

// New creates an email client from the app config.
func New(cfg *config.Config) *Client {
	return &Client{
		resend:   resend.NewClient(cfg.Email.APIKey),
		from:     cfg.Email.From,
		fromName: cfg.Email.FromName,
		baseURL:  cfg.App.BaseURL,
	}
}

// fromAddr renders the From header. net/mail handles RFC 5322 display-name
// quoting/encoding, so a misconfigured EMAIL_FROM_NAME containing specials
// (commas, quotes, angle brackets) still yields a well-formed header.
func (c *Client) fromAddr() string {
	return (&mail.Address{Name: c.fromName, Address: c.from}).String()
}

// SendOrderConfirmation sends the order confirmation email to the customer.
func (c *Client) SendOrderConfirmation(ctx context.Context, to, orderID, itemNames, total, shippingAddress string) error {
	html := orderConfirmationHTML(orderID, itemNames, total, shippingAddress, c.baseURL)
	_, err := c.resend.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    c.fromAddr(),
		To:      []string{to},
		Subject: fmt.Sprintf("Your UhhCraft order is in! (#%s)", shortID(orderID)),
		Html:    html,
	})
	return err
}

// SendWelcome sends a welcome email to a newly created account.
func (c *Client) SendWelcome(ctx context.Context, to string) error {
	_, err := c.resend.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    c.fromAddr(),
		To:      []string{to},
		Subject: "Welcome to UhhCraft! 🦊",
		Html:    welcomeHTML(c.baseURL),
	})
	return err
}

// SendEmailVerification sends an email verification link.
func (c *Client) SendEmailVerification(ctx context.Context, to, token string) error {
	link := fmt.Sprintf("%s/account/verify-email?token=%s", c.baseURL, url.QueryEscape(token))
	_, err := c.resend.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    c.fromAddr(),
		To:      []string{to},
		Subject: "Verify your UhhCraft email",
		Html:    verifyEmailHTML(link, c.baseURL),
	})
	return err
}

// SendPasswordReset sends a password reset link.
func (c *Client) SendPasswordReset(ctx context.Context, to, token string) error {
	link := fmt.Sprintf("%s/account/reset-password?token=%s", c.baseURL, url.QueryEscape(token))
	_, err := c.resend.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    c.fromAddr(),
		To:      []string{to},
		Subject: "Reset your UhhCraft password",
		Html:    passwordResetHTML(link, c.baseURL),
	})
	return err
}

// SendOrderShipped sends a shipping notification with tracking info.
func (c *Client) SendOrderShipped(ctx context.Context, to, orderID, trackingNumber, carrier string) error {
	_, err := c.resend.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    c.fromAddr(),
		To:      []string{to},
		Subject: fmt.Sprintf("Your UhhCraft order is on its way! (#%s)", shortID(orderID)),
		Html:    orderShippedHTML(orderID, trackingNumber, carrier, c.baseURL),
	})
	return err
}

// SendAbandonedCart sends a reminder that items are waiting in the cart.
func (c *Client) SendAbandonedCart(ctx context.Context, to string) error {
	_, err := c.resend.Emails.SendWithContext(ctx, &resend.SendEmailRequest{
		From:    c.fromAddr(),
		To:      []string{to},
		Subject: "You left something behind at UhhCraft",
		Html:    abandonedCartHTML(c.baseURL),
	})
	return err
}

// ── Email HTML templates ───────────────────────────────────────────────────────
// Inline CSS for email client compatibility.
// All use the UhhCraft brand: orange header, Nunito-adjacent fonts, warm tone.

const emailWrapper = `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#FAFAF7;font-family:'Arial Rounded MT Bold',Arial,sans-serif;">
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#FAFAF7;">
<tr><td align="center" style="padding:32px 16px;">
<table width="100%%" cellpadding="0" cellspacing="0" style="max-width:560px;">
  <!-- Header -->
  <tr><td style="background:#E8732A;padding:24px 32px;border-radius:16px 16px 0 0;">
    <span style="font-size:22px;font-weight:900;color:#201E1A;">🦊 UhhCraft</span>
  </td></tr>
  <!-- Body -->
  <tr><td style="background:#ffffff;padding:32px;border-radius:0 0 16px 16px;">
    %s
  </td></tr>
  <!-- Footer -->
  <tr><td style="padding:16px 32px;text-align:center;">
    <p style="font-size:12px;color:#9B9892;">© UhhCraft · Unique, one of a kind</p>
    <p style="font-size:11px;color:#BBB7AF;margin-top:4px;">
      <a href="%s/legal/privacy" style="color:#BBB7AF;">Privacy</a> ·
      <a href="%s/legal/terms" style="color:#BBB7AF;">Terms</a>
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>`

func wrap(content, baseURL string) string {
	return fmt.Sprintf(emailWrapper, content, baseURL, baseURL)
}

func btn(text, href string) string {
	return fmt.Sprintf(
		`<a href="%s" style="display:inline-block;padding:14px 28px;background:#E8732A;color:#201E1A;font-weight:700;font-size:15px;border-radius:10px;text-decoration:none;">%s</a>`,
		href, text,
	)
}

func orderConfirmationHTML(orderID, items, total, address, baseURL string) string {
	body := fmt.Sprintf(`
<h1 style="font-size:24px;font-weight:800;color:#201E1A;margin:0 0 8px;">Your order is in! 🎉</h1>
<p style="color:#5A5752;margin:0 0 24px;">We're getting started on it right away. Expect your item in 5–7 business days.</p>
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#F5F4F0;border-radius:10px;margin-bottom:24px;">
  <tr><td style="padding:16px 20px;">
    <p style="margin:0 0 8px;font-size:13px;color:#9B9892;text-transform:uppercase;letter-spacing:.05em;">ORDER</p>
    <p style="margin:0;font-size:14px;font-weight:700;color:#201E1A;">#%s</p>
  </td><td style="padding:16px 20px;" align="right">
    <p style="margin:0 0 8px;font-size:13px;color:#9B9892;text-transform:uppercase;letter-spacing:.05em;">TOTAL</p>
    <p style="margin:0;font-size:14px;font-weight:700;color:#201E1A;">%s</p>
  </td></tr>
</table>
<p style="color:#5A5752;font-size:14px;margin:0 0 24px;">Items: %s</p>
<p style="color:#5A5752;font-size:14px;margin:0 0 24px;">Shipping to: %s</p>
<p>%s</p>
<p style="color:#9B9892;font-size:13px;margin-top:32px;">You'll get another email when it ships. Questions? Reply to this email.</p>`,
		shortID(orderID), html.EscapeString(total), html.EscapeString(items), html.EscapeString(address),
		btn("View Order", baseURL+"/order/"+orderID),
	)
	return wrap(body, baseURL)
}

func welcomeHTML(baseURL string) string {
	body := fmt.Sprintf(`
<h1 style="font-size:24px;font-weight:800;color:#201E1A;margin:0 0 8px;">Welcome to UhhCraft! 🦊</h1>
<p style="color:#5A5752;margin:0 0 16px;">Every item we make is one of a kind. Browse what others have created, or make something totally your own.</p>
<ul style="color:#5A5752;padding-left:20px;margin:0 0 24px;">
  <li style="margin-bottom:8px;"><strong>Priority manufacturing</strong> — your orders jump the queue</li>
  <li style="margin-bottom:8px;"><strong>Saved designs</strong> — your last 10 generations, always ready</li>
  <li style="margin-bottom:8px;"><strong>Order history</strong> — reorder anything in seconds</li>
  <li><strong>Loyalty discounts</strong> — the more you order, the more you save</li>
</ul>
<p>%s</p>`,
		btn("Start Creating", baseURL+"/generate"),
	)
	return wrap(body, baseURL)
}

func verifyEmailHTML(link, baseURL string) string {
	body := fmt.Sprintf(`
<h1 style="font-size:24px;font-weight:800;color:#201E1A;margin:0 0 8px;">Verify your email</h1>
<p style="color:#5A5752;margin:0 0 24px;">Click below to verify your email address. This link expires in 24 hours.</p>
<p>%s</p>
<p style="color:#9B9892;font-size:13px;margin-top:32px;">If you didn't create an UhhCraft account, you can safely ignore this email.</p>`,
		btn("Verify Email", link),
	)
	return wrap(body, baseURL)
}

func passwordResetHTML(link, baseURL string) string {
	body := fmt.Sprintf(`
<h1 style="font-size:24px;font-weight:800;color:#201E1A;margin:0 0 8px;">Reset your password</h1>
<p style="color:#5A5752;margin:0 0 24px;">Click below to choose a new password. This link expires in 1 hour.</p>
<p>%s</p>
<p style="color:#9B9892;font-size:13px;margin-top:32px;">If you didn't request this, you can safely ignore this email — your password hasn't changed.</p>`,
		btn("Reset Password", link),
	)
	return wrap(body, baseURL)
}

func orderShippedHTML(orderID, tracking, carrier, baseURL string) string {
	body := fmt.Sprintf(`
<h1 style="font-size:24px;font-weight:800;color:#201E1A;margin:0 0 8px;">It's on its way! 📦</h1>
<p style="color:#5A5752;margin:0 0 24px;">Your UhhCraft order #%s has shipped.</p>
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#F5F4F0;border-radius:10px;margin-bottom:24px;">
  <tr><td style="padding:16px 20px;">
    <p style="margin:0 0 4px;font-size:13px;color:#9B9892;">Carrier</p>
    <p style="margin:0;font-weight:700;color:#201E1A;">%s</p>
  </td><td style="padding:16px 20px;">
    <p style="margin:0 0 4px;font-size:13px;color:#9B9892;">Tracking</p>
    <p style="margin:0;font-weight:700;color:#201E1A;">%s</p>
  </td></tr>
</table>
<p>%s</p>`,
		shortID(orderID), html.EscapeString(carrier), html.EscapeString(tracking),
		btn("Track Order", baseURL+"/order/"+orderID),
	)
	return wrap(body, baseURL)
}

func abandonedCartHTML(baseURL string) string {
	body := fmt.Sprintf(`
<h1 style="font-size:24px;font-weight:800;color:#201E1A;margin:0 0 8px;">You left something behind 👀</h1>
<p style="color:#5A5752;margin:0 0 24px;">You got as far as your cart — your item is waiting. Come back and finish your order.</p>
<p>%s</p>
<p style="color:#9B9892;font-size:13px;margin-top:32px;">If you've changed your mind, that's totally fine. No pressure.</p>`,
		btn("Return to Cart", baseURL+"/cart"),
	)
	return wrap(body, baseURL)
}
