// Package auth handles user authentication, sessions, and password management.
package auth

import (
	"net/http"

	"github.com/alexedwards/scs/v2"
)

const (
	sessionKeyUserID    = "user_id"
	sessionKeyUserEmail = "user_email"
	sessionKeyUserRole  = "user_role"
)

// SetUser writes auth state into the session after successful login.
// Rotates the session token to prevent fixation.
func SetUser(sessions *scs.SessionManager, r *http.Request, id, email, role string) error {
	if err := sessions.RenewToken(r.Context()); err != nil {
		return err
	}
	sessions.Put(r.Context(), sessionKeyUserID, id)
	sessions.Put(r.Context(), sessionKeyUserEmail, email)
	sessions.Put(r.Context(), sessionKeyUserRole, role)
	return nil
}

// ClearUser removes auth state from the session on sign-out.
func ClearUser(sessions *scs.SessionManager, r *http.Request) error {
	return sessions.Destroy(r.Context())
}

// UserID returns the authenticated user ID from the session, or "".
func UserID(sessions *scs.SessionManager, r *http.Request) string {
	id, _ := sessions.Get(r.Context(), sessionKeyUserID).(string)
	return id
}

// UserRole returns the authenticated user role, or "".
func UserRole(sessions *scs.SessionManager, r *http.Request) string {
	role, _ := sessions.Get(r.Context(), sessionKeyUserRole).(string)
	return role
}

// IsAuthenticated reports whether the request has a valid user session.
func IsAuthenticated(sessions *scs.SessionManager, r *http.Request) bool {
	return UserID(sessions, r) != ""
}

// IsAdmin reports whether the authenticated user is an admin.
func IsAdmin(sessions *scs.SessionManager, r *http.Request) bool {
	return UserRole(sessions, r) == "admin"
}

// SessionID returns the guest session identifier — the SCS session token from
// the session cookie — used to scope cart and generation history for
// unauthenticated users. Returns "" if no session cookie exists yet; a caller
// that needs a stable guest key must persist something to the session first to
// mint a token (see the generation handler).
func SessionID(r *http.Request) string {
	if c, err := r.Cookie("scs.session.token"); err == nil {
		return c.Value
	}
	return ""
}
