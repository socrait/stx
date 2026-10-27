#!/bin/sh
# Generates /tmp/users.acl from env vars then starts redis-server — Compose
# can't substitute ${VAR} placeholders inside mounted file content.
set -eu

cat > /tmp/users.acl <<EOF
user ${REDIS_USERNAME:-admin} allcommands allkeys allchannels on >${REDIS_PASSWORD}
user ${REDIS_APP_USERNAME:-app} +@all -@admin -@dangerous allkeys allchannels on >${REDIS_APP_PASSWORD}
user default off
EOF
chmod 600 /tmp/users.acl

exec redis-server /usr/local/etc/redis/redis.conf --aclfile /tmp/users.acl
