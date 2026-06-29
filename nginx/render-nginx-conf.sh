#!/bin/sh
set -eu

OUTPUT=/etc/nginx/nginx.conf

: "${EXTERNAL_DOMAIN:=}"
: "${INTERNAL_DOMAIN:=localhost}"

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
        server mcp-gateway:8000;
    }

    server {
        listen 80 default_server;
        server_name ${INTERNAL_DOMAIN} localhost 127.0.0.1;

EOF
write_proxy_locations >> "$OUTPUT"
cat >> "$OUTPUT" <<'EOF'
    }
EOF

if [ -n "$EXTERNAL_DOMAIN" ]; then
  CERT="/etc/nginx/ssl/$EXTERNAL_DOMAIN/fullchain.pem"
  KEY="/etc/nginx/ssl/$EXTERNAL_DOMAIN/privkey.pem"
  if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    cat >> "$OUTPUT" <<EOF

    server {
        listen 443 ssl http2;
        server_name $EXTERNAL_DOMAIN;

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

    server {
        listen 80;
        server_name $EXTERNAL_DOMAIN;
        return 301 https://\$host\$request_uri;
    }
EOF
  else
    cat >> "$OUTPUT" <<EOF

    server {
        listen 80;
        server_name $EXTERNAL_DOMAIN;

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
