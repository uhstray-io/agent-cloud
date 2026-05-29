package auth

import (
	"crypto/rand"
	"encoding/hex"
	"time"

	"golang.org/x/crypto/bcrypt"
)

const bcryptCost = 12

// HashPassword returns the bcrypt hash of a plaintext password.
func HashPassword(plain string) (string, error) {
	b, err := bcrypt.GenerateFromPassword([]byte(plain), bcryptCost)
	return string(b), err
}

// CheckPassword reports whether plain matches the stored bcrypt hash.
func CheckPassword(plain, hash string) bool {
	return bcrypt.CompareHashAndPassword([]byte(hash), []byte(plain)) == nil
}

// GenerateToken returns a cryptographically secure random hex token.
func GenerateToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// EmailVerificationTTL is how long an email verification link stays valid.
const EmailVerificationTTL = 24 * time.Hour

// PasswordResetTTL is how long a password reset link stays valid.
const PasswordResetTTL = 1 * time.Hour
