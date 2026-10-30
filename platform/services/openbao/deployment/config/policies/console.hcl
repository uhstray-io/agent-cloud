# console: the Dispatch Console's own read access (plan 17, D27).
# Bound to the console AppRole (provision-console-approle.yml, role name `console`). The console logs in at
# run time to read its own secrets (secret/services/console/*, e.g. the GitHub token) and OpenBao metadata
# for the Secrets page. Nothing beyond these two grants: no read outside its own data path, no write, no
# token renewal, no secret_id generation. The monthly secret_id rotation is rotate-console-credentials.yml's job.

path "secret/data/services/console/*" {
  capabilities = ["read"]
}

path "secret/metadata/*" {
  capabilities = ["list", "read"]
}
