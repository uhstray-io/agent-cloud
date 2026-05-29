package auth

import (
	"context"
	"errors"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/a-h/templ"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/wisward/uhhcraft/internal/app"
	"github.com/wisward/uhhcraft/internal/db"
	"github.com/wisward/uhhcraft/web/templates/pages"
)

// safeNext returns next if it is a safe same-origin relative path, else "/account".
// Rejects absolute URLs, scheme-relative URLs (//host), and backslash tricks so
// a crafted ?next= cannot turn login into an open redirect.
func safeNext(next string) string {
	if next == "" || next[0] != '/' {
		return "/account"
	}
	if strings.HasPrefix(next, "//") || strings.HasPrefix(next, "/\\") {
		return "/account"
	}
	return next
}

// clientIP derives a stable per-client key from forwarded headers (set by the
// central Caddy proxy) or the connection peer, stripping the ephemeral source
// port so a client can't dodge throttling by opening new connections.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.IndexByte(xff, ','); i >= 0 {
			xff = xff[:i]
		}
		return strings.TrimSpace(xff)
	}
	if xr := strings.TrimSpace(r.Header.Get("X-Real-IP")); xr != "" {
		return xr
	}
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}

// SignInHandler renders the sign-in form.
func SignInHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if IsAuthenticated(a.Sessions, r) {
			http.Redirect(w, r, "/account", http.StatusSeeOther)
			return
		}
		next := r.URL.Query().Get("next")
		render(w, r, pages.SignInPage(pages.SignInData{Next: next}))
	}
}

// SignInPostHandler processes sign-in form submission.
func SignInPostHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}

		emailAddr := strings.TrimSpace(strings.ToLower(r.FormValue("email")))
		password := r.FormValue("password")
		next := safeNext(r.FormValue("next"))
		ip := clientIP(r)

		ok, err := a.RateLimit.AllowLogin(r.Context(), ip)
		if err != nil {
			a.Logger.Error("login rate limit error", "err", err)
		}
		if !ok {
			render(w, r, pages.SignInPage(pages.SignInData{
				Email: emailAddr,
				Error: "Too many login attempts. Please wait a few minutes and try again.",
				Next:  next,
			}))
			return
		}

		user, err := a.Queries.GetUserByEmail(r.Context(), emailAddr)
		if err != nil || !CheckPassword(password, user.PasswordHash) {
			render(w, r, pages.SignInPage(pages.SignInData{
				Email: emailAddr,
				Error: "Email or password is incorrect.",
				Next:  next,
			}))
			return
		}

		if !user.EmailVerified {
			render(w, r, pages.SignInPage(pages.SignInData{
				Email: emailAddr,
				Error: "Please verify your email address first. Check your inbox.",
				Next:  next,
			}))
			return
		}

		if err := SetUser(a.Sessions, r, db.UUIDToString(user.ID), user.Email, user.Role); err != nil {
			http.Error(w, "session error", http.StatusInternalServerError)
			return
		}
		if err := a.RateLimit.ResetLogin(r.Context(), ip); err != nil {
			a.Logger.Error("login rate reset", "err", err)
		}

		sessionCookie, _ := r.Cookie("session")
		if sessionCookie != nil {
			_ = a.Queries.MigrateGuestCartToUser(r.Context(), db.MigrateGuestCartToUserParams{
				SessionID: pgtype.Text{String: sessionCookie.Value, Valid: true},
				UserID:    user.ID,
			})
		}

		http.Redirect(w, r, next, http.StatusSeeOther)
	}
}

// SignUpHandler renders the sign-up form.
func SignUpHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if IsAuthenticated(a.Sessions, r) {
			http.Redirect(w, r, "/account", http.StatusSeeOther)
			return
		}
		render(w, r, pages.SignUpPage(pages.SignUpData{}))
	}
}

// SignUpPostHandler processes sign-up form submission.
func SignUpPostHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}

		emailAddr := strings.TrimSpace(strings.ToLower(r.FormValue("email")))
		password := r.FormValue("password")
		confirm := r.FormValue("confirm_password")
		ageGate := r.FormValue("age_gate") == "on"

		data := pages.SignUpData{Email: emailAddr}
		switch {
		case emailAddr == "" || !strings.Contains(emailAddr, "@"):
			data.Error = "Please enter a valid email address."
		case len(password) < 8:
			data.Error = "Password must be at least 8 characters."
		case password != confirm:
			data.Error = "Passwords don't match."
		case !ageGate:
			data.Error = "You must confirm that you are 13 years of age or older."
		}
		if data.Error != "" {
			render(w, r, pages.SignUpPage(data))
			return
		}

		hash, err := HashPassword(password)
		if err != nil {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}

		user, err := a.Queries.CreateUser(r.Context(), db.CreateUserParams{
			Email:        emailAddr,
			PasswordHash: hash,
			Role:         "user",
		})
		if err != nil {
			if isUniqueViolation(err) {
				data.Error = "An account with that email already exists."
			} else {
				data.Error = "Something went wrong — please try again."
				a.Logger.Error("create user", "err", err)
			}
			render(w, r, pages.SignUpPage(data))
			return
		}

		// The verification token must be created durably, or the account is
		// unusable (the user can never verify). Treat token-creation failure
		// as a hard error so they retry rather than landing on a success page
		// with a dead account.
		token, err := GenerateToken()
		if err != nil {
			a.Logger.Error("generate verification token", "err", err)
			data.Error = "Something went wrong — please try again."
			render(w, r, pages.SignUpPage(data))
			return
		}
		if _, err := a.Queries.CreateEmailVerificationToken(r.Context(), db.CreateEmailVerificationTokenParams{
			UserID:    user.ID,
			Token:     token,
			ExpiresAt: pgtype.Timestamptz{Time: time.Now().Add(EmailVerificationTTL), Valid: true},
		}); err != nil {
			a.Logger.Error("create verification token", "err", err)
			data.Error = "Something went wrong — please try again."
			render(w, r, pages.SignUpPage(data))
			return
		}

		// Email delivery failures are logged but non-fatal: the account+token
		// exist, so the user can request a resend. We don't fail sign-up on a
		// transient mail error.
		if err := a.Email.SendEmailVerification(r.Context(), emailAddr, token); err != nil {
			a.Logger.Error("send verification email", "err", err)
		}
		if err := a.Email.SendWelcome(r.Context(), emailAddr); err != nil {
			a.Logger.Error("send welcome email", "err", err)
		}

		render(w, r, pages.SignUpSuccessPage(emailAddr))
	}
}

// SignOutHandler destroys the session and redirects home.
func SignOutHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		_ = ClearUser(a.Sessions, r)
		http.Redirect(w, r, "/", http.StatusSeeOther)
	}
}

// VerifyEmailHandler processes an email verification link.
func VerifyEmailHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("token")
		if token == "" {
			http.Redirect(w, r, "/account/sign-in", http.StatusSeeOther)
			return
		}

		// Atomically consume the token (single UPDATE ... RETURNING) so a
		// double-clicked link or concurrent request can't verify twice.
		userID, err := a.Queries.ConsumeEmailVerificationToken(r.Context(), token)
		if err != nil {
			if !errors.Is(err, pgx.ErrNoRows) {
				a.Logger.Error("consume verification token", "err", err)
			}
			render(w, r, pages.VerifyEmailPage(false))
			return
		}

		// The token is now spent; the verification flag write must succeed or
		// the user is left unable to sign in. Report failure rather than a
		// false success.
		if err := a.Queries.UpdateUserEmailVerified(r.Context(), userID); err != nil {
			a.Logger.Error("mark email verified", "err", err)
			render(w, r, pages.VerifyEmailPage(false))
			return
		}

		render(w, r, pages.VerifyEmailPage(true))
	}
}

// ForgotPasswordHandler renders the forgot-password form.
func ForgotPasswordHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		render(w, r, pages.ForgotPasswordPage(pages.ForgotPasswordData{}))
	}
}

// ForgotPasswordPostHandler sends a password reset email.
func ForgotPasswordPostHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		emailAddr := strings.TrimSpace(strings.ToLower(r.FormValue("email")))

		// Bounded background work: a single goroutine with its own timeout
		// context (not context.Background()) so the DB/email calls can't hang
		// indefinitely or pile up unbounded under load. We still respond
		// immediately and identically whether or not the account exists, so
		// the response time doesn't leak account existence.
		go func(email string) {
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()

			user, err := a.Queries.GetUserByEmail(ctx, email)
			if err != nil {
				return
			}
			token, err := GenerateToken()
			if err != nil {
				a.Logger.Error("generate reset token", "err", err)
				return
			}
			if _, err := a.Queries.CreatePasswordResetToken(ctx, db.CreatePasswordResetTokenParams{
				UserID:    user.ID,
				Token:     token,
				ExpiresAt: pgtype.Timestamptz{Time: time.Now().Add(PasswordResetTTL), Valid: true},
			}); err != nil {
				a.Logger.Error("create reset token", "err", err)
				return
			}
			if err := a.Email.SendPasswordReset(ctx, email, token); err != nil {
				a.Logger.Error("send reset email", "err", err)
			}
		}(emailAddr)

		render(w, r, pages.ForgotPasswordPage(pages.ForgotPasswordData{Sent: true, Email: emailAddr}))
	}
}

// ResetPasswordHandler renders the password reset form.
func ResetPasswordHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("token")
		if token == "" {
			http.Redirect(w, r, "/account/forgot-password", http.StatusSeeOther)
			return
		}
		if _, err := a.Queries.GetPasswordResetToken(r.Context(), token); err != nil {
			render(w, r, pages.ResetPasswordPage(pages.ResetPasswordData{
				Token: token,
				Error: "This reset link has expired or already been used. Request a new one.",
			}))
			return
		}
		render(w, r, pages.ResetPasswordPage(pages.ResetPasswordData{Token: token}))
	}
}

// ResetPasswordPostHandler processes the password reset form.
func ResetPasswordPostHandler(a *app.App) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := r.FormValue("token")
		password := r.FormValue("password")
		confirm := r.FormValue("confirm_password")

		data := pages.ResetPasswordData{Token: token}
		switch {
		case len(password) < 8:
			data.Error = "Password must be at least 8 characters."
		case password != confirm:
			data.Error = "Passwords don't match."
		}
		if data.Error != "" {
			render(w, r, pages.ResetPasswordPage(data))
			return
		}

		hash, err := HashPassword(password)
		if err != nil {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}

		// Atomically consume the reset token first; only if that succeeds do
		// we change the password. This closes the replay window and guarantees
		// the token can't be reused.
		userID, err := a.Queries.ConsumePasswordResetToken(r.Context(), token)
		if err != nil {
			if !errors.Is(err, pgx.ErrNoRows) {
				a.Logger.Error("consume reset token", "err", err)
			}
			data.Error = "This reset link has expired or already been used. Request a new one."
			render(w, r, pages.ResetPasswordPage(data))
			return
		}

		// The token is spent; the password write must succeed or the user is
		// locked out with a now-dead token. Surface failure instead of a false
		// success page.
		if err := a.Queries.UpdateUserPassword(r.Context(), db.UpdateUserPasswordParams{
			ID:           userID,
			PasswordHash: hash,
		}); err != nil {
			a.Logger.Error("update password", "err", err)
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}

		render(w, r, pages.ResetPasswordSuccessPage())
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func render(w http.ResponseWriter, r *http.Request, c templ.Component) {
	if err := c.Render(r.Context(), w); err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
	}
}

func isUniqueViolation(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	return strings.Contains(s, "unique") || strings.Contains(s, "duplicate")
}
