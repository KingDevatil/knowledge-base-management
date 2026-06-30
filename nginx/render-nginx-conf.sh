#!/bin/sh
set -eu

OUTPUT=/etc/nginx/nginx.conf
SERVICE_ENV_FILE="${SERVICE_ENV_FILE:-/app/data/config/service.env}"

read_env_value() {
  key="$1"
  if [ ! -f "$SERVICE_ENV_FILE" ]; then
    return 0
  fi
  value="$(grep -m 1 "^${key}=" "$SERVICE_ENV_FILE" 2>/dev/null | sed "s/^${key}=//" || true)"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

file_value="$(read_env_value EXTERNAL_DOMAIN)"; [ -n "$file_value" ] && EXTERNAL_DOMAIN="$file_value"
file_value="$(read_env_value INTERNAL_DOMAIN)"; [ -n "$file_value" ] && INTERNAL_DOMAIN="$file_value"
file_value="$(read_env_value REVERSE_PROXY_ENABLED)"; [ -n "$file_value" ] && REVERSE_PROXY_ENABLED="$file_value"
file_value="$(read_env_value REVERSE_PROXY_DOMAIN)"; [ -n "$file_value" ] && REVERSE_PROXY_DOMAIN="$file_value"
file_value="$(read_env_value REVERSE_PROXY_UPSTREAM_HOST)"; [ -n "$file_value" ] && REVERSE_PROXY_UPSTREAM_HOST="$file_value"
file_value="$(read_env_value REVERSE_PROXY_UPSTREAM_PORT)"; [ -n "$file_value" ] && REVERSE_PROXY_UPSTREAM_PORT="$file_value"
file_value="$(read_env_value REVERSE_PROXY_SSL_CERT_FILE)"; [ -n "$file_value" ] && REVERSE_PROXY_SSL_CERT_FILE="$file_value"
file_value="$(read_env_value REVERSE_PROXY_SSL_KEY_FILE)"; [ -n "$file_value" ] && REVERSE_PROXY_SSL_KEY_FILE="$file_value"
file_value="$(read_env_value REVERSE_PROXY_FORCE_HTTPS)"; [ -n "$file_value" ] && REVERSE_PROXY_FORCE_HTTPS="$file_value"

: "${EXTERNAL_DOMAIN:=}"
: "${INTERNAL_DOMAIN:=localhost}"
: "${REVERSE_PROXY_ENABLED:=true}"
: "${REVERSE_PROXY_DOMAIN:=$EXTERNAL_DOMAIN}"
: "${REVERSE_PROXY_UPSTREAM_HOST:=mcp-gateway}"
: "${REVERSE_PROXY_UPSTREAM_PORT:=8000}"
: "${REVERSE_PROXY_SSL_CERT_FILE:=}"
: "${REVERSE_PROXY_SSL_KEY_FILE:=}"
: "${REVERSE_PROXY_FORCE_HTTPS:=true}"

case "$(printf '%s' "$REVERSE_PROXY_ENABLED" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on|enabled) REVERSE_PROXY_ENABLED=true ;;
  *) REVERSE_PROXY_ENABLED=false ;;
esac

write_proxy_locations() {
  cat <<'EOF'
        location /sse {
            proxy_pass http://mcp_gateway;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 3600s;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-API-Key $http_x_api_key;
        }

        location /mcp {
            proxy_pass http://mcp_gateway;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 3600s;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-API-Key $http_x_api_key;
        }

        location / {
            proxy_pass http://mcp_gateway;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 3600s;
        }
EOF
}

cat > "$OUTPUT" <<EOF
events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    server_tokens off;

    types {
        text/event-stream sse;
    }

    map \$http_upgrade \$connection_upgrade {
        default upgrade;
        ''      '';
    }

    upstream mcp_gateway {
        server ${REVERSE_PROXY_UPSTREAM_HOST}:${REVERSE_PROXY_UPSTREAM_PORT};
    }

EOF

if [ "$REVERSE_PROXY_ENABLED" != "true" ]; then
  cat >> "$OUTPUT" <<'EOF'
    server {
        listen 80 default_server;
        server_name _;
        return 404;
    }
}
EOF
  nginx -t
  exit 0
fi

cat >> "$OUTPUT" <<EOF
    server {
        listen 80 default_server;
        server_name ${INTERNAL_DOMAIN} localhost 127.0.0.1;

EOF
write_proxy_locations >> "$OUTPUT"
cat >> "$OUTPUT" <<'EOF'
    }
EOF

if [ -n "$REVERSE_PROXY_DOMAIN" ]; then
  CERT="${REVERSE_PROXY_SSL_CERT_FILE:-/etc/nginx/ssl/$REVERSE_PROXY_DOMAIN/fullchain.pem}"
  KEY="${REVERSE_PROXY_SSL_KEY_FILE:-/etc/nginx/ssl/$REVERSE_PROXY_DOMAIN/privkey.pem}"
  if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    cat >> "$OUTPUT" <<EOF

    server {
        listen 443 ssl http2;
        server_name $REVERSE_PROXY_DOMAIN;

        ssl_certificate     $CERT;
        ssl_certificate_key $KEY;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers HIGH:!aNULL:!MD5;
        ssl_prefer_server_ciphers on;
        ssl_session_cache shared:SSL:10m;
        ssl_session_timeout 10m;

        add_header X-Frame-Options "SAMEORIGIN" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

EOF
    write_proxy_locations >> "$OUTPUT"
    cat >> "$OUTPUT" <<EOF
    }
EOF

    if [ "$REVERSE_PROXY_FORCE_HTTPS" = "true" ]; then
      cat >> "$OUTPUT" <<EOF
    server {
        listen 80;
        server_name $REVERSE_PROXY_DOMAIN;
        return 301 https://\$host\$request_uri;
    }
EOF
    fi
  else
    cat >> "$OUTPUT" <<EOF

    server {
        listen 80;
        server_name $REVERSE_PROXY_DOMAIN;

EOF
    write_proxy_locations >> "$OUTPUT"
    cat >> "$OUTPUT" <<'EOF'
    }
EOF
  fi
fi

cat >> "$OUTPUT" <<'EOF'
}
EOF

nginx -t
