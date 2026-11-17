# agent-cloud developer toolchain (LOCAL-DEV-DEPLOYMENT.md Phase 0B).
# Install: `brew bundle` from the repo root. `make local-preflight` verifies.

brew "ansible"
brew "ansible-lint"
brew "bats-core"
brew "cargo-nextest"  # Rust test runner for the hook lanes (plan 24); needs rustup's cargo; CI pins its own version
brew "gh"
brew "git"
brew "go"
brew "jq"
brew "make"
brew "parallel"       # GNU parallel: `bats --jobs` runs test files concurrently (plan 21, D4)
brew "podman"
brew "podman-compose"
brew "rsync"          # GNU rsync: place-monorepo's --delete must honour .gitignore (macOS ships openrsync, which does not)
brew "rustup"         # keg-only: put its bin on PATH (CONTRIBUTING.md, Tool Installation); platform/tests/rust-toolchain.toml picks the toolchain
brew "shellcheck"
brew "socat"          # local-dev privileged-port forwarder (make local-https)
brew "yamllint"

# Optional — only for root-requiring services (NetBox app tier profile):
# cask "docker"
