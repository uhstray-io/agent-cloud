#!/bin/bash
# Creates the Diode and Hydra databases and users on first PostgreSQL startup.
# This script runs inside the NetBox postgres container via docker-entrypoint-initdb.d.
# The DIODE_* and HYDRA_* variables come from env/postgres.env.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set diode_user="$DIODE_POSTGRES_USER" \
  --set diode_pass="$DIODE_POSTGRES_PASSWORD" \
  --set diode_db="$DIODE_POSTGRES_DB_NAME" \
  --set hydra_user="$HYDRA_POSTGRES_USER" \
  --set hydra_pass="$HYDRA_POSTGRES_PASSWORD" \
  --set hydra_db="$HYDRA_POSTGRES_DB_NAME" <<-'EOSQL'
    -- Diode database
    CREATE USER :diode_user WITH PASSWORD :'diode_pass';
    CREATE DATABASE :diode_db OWNER :diode_user;

    -- Hydra (OAuth2) database
    CREATE USER :hydra_user WITH PASSWORD :'hydra_pass';
    CREATE DATABASE :hydra_db OWNER :hydra_user;
EOSQL
