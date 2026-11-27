#!/bin/sh
# Executable in immutable image; the password lives only in the child process
# environment and never appears in argv, files or logs.
[ -n "${OWRT_REMOTE_SSH_PASSWORD:-}" ] || exit 1
printf '%s\n' "$OWRT_REMOTE_SSH_PASSWORD"
