#!/bin/sh
set -eu
mkdir -p /data/servers /data/backups /data/logs /data/uploads
if [ -S /var/run/docker.sock ]; then
  SOCKET_GID="$(stat -c '%g' /var/run/docker.sock)"
  if ! getent group "$SOCKET_GID" >/dev/null 2>&1; then
    groupadd -g "$SOCKET_GID" dockerhost
  fi
  USER_GROUP="$(getent group "$SOCKET_GID" | cut -d: -f1)"
  usermod -aG "$USER_GROUP" manager
fi
chown -R manager:manager /data
exec su -s /bin/sh manager -c "exec $*"
