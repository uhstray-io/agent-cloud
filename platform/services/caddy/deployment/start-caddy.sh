#!/bin/bash

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $0 -k <cloudflare_api_key> [application_options]"
    echo ""
    echo "Required:"
    echo "  -k <api_key>     CloudFlare API key"
    echo ""
    echo "Application Options (specify IP and domain for each):"
    echo "  -n <ip:domain[:port]>    NocoDB application"
    echo "  -w <ip:domain[:port]>    N8N workflow application"
    echo "  -p <ip:domain[:port]>    Postiz application"
    echo "  -s <ip:domain[:port]>    Superset application"
    echo "  -o <ip:domain[:port]>    O11Y (observability) application"
    echo "  -m <ip:domain[:port]>    Mixpost application"
    echo "  -b <ip:domain[:port]>    Wisbot application"
    echo "  -c <ip:domain[:port]>    Cloud/Collabora application"
    echo ""
    echo "Format: ip:domain[:port]"
    echo ""
    echo "Default Ports (used if not specified):"
    echo "  - NocoDB: 8080"
    echo "  - N8N: 5678"
    echo "  - Postiz: 5000"
    echo "  - Superset: 8088"
    echo "  - O11Y: 3000"
    echo "  - Mixpost: 9095"
    echo "  - Wisbot: 8080"
    echo "  - Cloud: 11000 (main), 3002 (websocket), 9980 (collabora)"
    echo ""
    echo "Examples:"
    echo "  # Using default ports:"
    echo "  $0 -k your_cf_api_key -n 192.168.1.100:nocodb.example.com -w 192.168.1.101:n8n.example.com"
    echo ""
    echo "  # Using custom ports:"
    echo "  $0 -k your_cf_api_key -n 192.168.1.100:nocodb.example.com:8081 -w 192.168.1.101:n8n.example.com:5679"
    exit 1
}

if [ $# -eq 0 ]; then
    usage
fi

CLOUDFLARE_API_KEY=""
NOCODB_CONFIG=""
N8N_CONFIG=""
POSTIZ_CONFIG=""
SUPERSET_CONFIG=""
O11Y_CONFIG=""
MIXPOST_CONFIG=""
WISBOT_CONFIG=""
CLOUD_CONFIG=""

while getopts "k:n:w:p:s:o:m:b:c:h" opt; do
    case $opt in
        k)
            CLOUDFLARE_API_KEY="$OPTARG"
            ;;
        n)
            NOCODB_CONFIG="$OPTARG"
            ;;
        w)
            N8N_CONFIG="$OPTARG"
            ;;
        p)
            POSTIZ_CONFIG="$OPTARG"
            ;;
        s)
            SUPERSET_CONFIG="$OPTARG"
            ;;
        o)
            O11Y_CONFIG="$OPTARG"
            ;;
        m)
            MIXPOST_CONFIG="$OPTARG"
            ;;
        b)
            WISBOT_CONFIG="$OPTARG"
            ;;
        c)
            CLOUD_CONFIG="$OPTARG"
            ;;
        h)
            usage
            ;;
        \?)
            echo "Invalid option: -$OPTARG" >&2
            usage
            ;;
    esac
done

if [ -z "$CLOUDFLARE_API_KEY" ]; then
    echo "Error: CloudFlare API key is required (-k option)"
    exit 1
fi

# Check if at least one application is configured
if [ -z "$NOCODB_CONFIG" ] && [ -z "$N8N_CONFIG" ] && [ -z "$POSTIZ_CONFIG" ] && [ -z "$SUPERSET_CONFIG" ] && [ -z "$O11Y_CONFIG" ] && [ -z "$MIXPOST_CONFIG" ] && [ -z "$WISBOT_CONFIG" ] && [ -z "$CLOUD_CONFIG" ]; then
    echo "Error: At least one application must be configured"
    usage
fi

validate_config() {
    local config="$1"
    local app_name="$2"

    if [ -n "$config" ]; then
        IFS=':' read -r ip domain port <<< "$config"

        if [ -z "$ip" ] || [ -z "$domain" ]; then
            echo "Error: Invalid $app_name configuration: $config"
            echo "Expected format: ip:domain[:port]"
            exit 1
        fi

        # Validate IP format (basic check)
        if ! [[ $ip =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            echo "Error: Invalid IP address format for $app_name: $ip"
            exit 1
        fi

        # Validate domain format (basic check)
        if ! [[ $domain =~ ^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$ ]]; then
            echo "Error: Invalid domain format for $app_name: $domain"
            exit 1
        fi
    fi
}

# Validate all configurations
validate_config "$NOCODB_CONFIG" "NocoDB"
validate_config "$N8N_CONFIG" "N8N"
validate_config "$POSTIZ_CONFIG" "Postiz"
validate_config "$SUPERSET_CONFIG" "Superset"
validate_config "$O11Y_CONFIG" "O11Y"
validate_config "$MIXPOST_CONFIG" "Mixpost"
validate_config "$WISBOT_CONFIG" "Wisbot"
validate_config "$CLOUD_CONFIG" "Cloud"

echo "Setting up environment variables for Caddy..."

# Set CloudFlare API key
export CLOUDFLARE_API_KEY="$CLOUDFLARE_API_KEY"

set_app_env() {
    local config="$1"
    local app_prefix="$2"
    local default_port="$3"

    if [ -n "$config" ]; then
        IFS=':' read -r ip domain port <<< "$config"

        # Use default port if not provided, or use custom port if specified
        if [ -z "$port" ]; then
            port="$default_port"
        fi

        echo "Setting environment variables for ${app_prefix}: $domain -> $ip:$port"

        export "${app_prefix}_IP=$ip"
        export "${app_prefix}_DOMAIN=$domain"
        export "${app_prefix}_PORT=$port"

        # Special handling for Cloud app with multiple ports
        if [ "$app_prefix" = "CLOUD" ]; then
            # For Cloud app, if custom port provided, use it for main port, otherwise use defaults
            if [ "$port" != "$default_port" ]; then
                export CLOUD_PORT_MAIN="$port"
            else
                export CLOUD_PORT_MAIN="11000"
            fi
            export CLOUD_PORT_WS="3002"
            export CLOUD_PORT_COLLABORA="9980"
        fi
    fi
}

# Set environment variables for each configured application
set_app_env "$NOCODB_CONFIG" "NOCODB" "8080"
set_app_env "$N8N_CONFIG" "N8N" "5678"
set_app_env "$POSTIZ_CONFIG" "POSTIZ" "5000"
set_app_env "$SUPERSET_CONFIG" "SUPERSET" "8088"
set_app_env "$O11Y_CONFIG" "O11Y" "3000"
set_app_env "$MIXPOST_CONFIG" "MIXPOST" "9095"
set_app_env "$WISBOT_CONFIG" "WISBOT" "8080"
set_app_env "$CLOUD_CONFIG" "CLOUD" "11000"

echo "Environment variables configured successfully"
echo ""
echo "Starting Caddy with docker-compose..."

cd "$SCRIPT_DIR"

docker compose down --remove-orphans 2>/dev/null || true

docker compose up -d

echo ""
echo "Caddy started successfully!"
echo "Applications configured:"

show_config() {
    local config="$1"
    local app_name="$2"
    local default_port="$3"

    if [ -n "$config" ]; then
        IFS=':' read -r ip domain port <<< "$config"
        if [ -z "$port" ]; then
            port="$default_port"
        fi
        echo "  - $app_name: https://$domain -> $ip:$port"
    fi
}

show_config "$NOCODB_CONFIG" "NocoDB" "8080"
show_config "$N8N_CONFIG" "N8N" "5678"
show_config "$POSTIZ_CONFIG" "Postiz" "5000"
show_config "$SUPERSET_CONFIG" "Superset" "8088"
show_config "$O11Y_CONFIG" "O11Y" "3000"
show_config "$MIXPOST_CONFIG" "Mixpost" "9095"
show_config "$WISBOT_CONFIG" "Wisbot" "8080"
show_config "$CLOUD_CONFIG" "Cloud" "11000"
