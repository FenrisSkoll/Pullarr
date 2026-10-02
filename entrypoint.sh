#!/usr/bin/env bash
set -e

PUID=${PUID:-0}
PGID=${PGID:-0}

DB_DIR="/app/db"
LOG_DIR="/app/logs"
TD_DIR="/app/temp_downloads"

if [ "$PUID" = "0" ]
then
    # Stay as root user
    echo "Running as root"
    exec "$@"

else
    # Switch to non-root user
    echo "Preparing Pullarr to run as $PUID:$PGID..."

    groupmod -o -g "$PGID" kapowarr
    usermod -o -u "$PUID" -g "$PGID" kapowarr

    echo "Ensuring ownership..."
    chown -R kapowarr:kapowarr "$DB_DIR" "$LOG_DIR" "$TD_DIR" || {
        echo "Failed to update ownership to $PUID:$PGID"
        exit 1
    }

    echo "Running as $PUID:$PGID"
    exec setpriv --reuid=kapowarr --regid=kapowarr --init-groups "$@"
fi
