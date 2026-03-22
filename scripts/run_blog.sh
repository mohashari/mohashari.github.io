#!/usr/bin/env bash
set -euo pipefail

BLOG_DIR="/home/muklis/Documents/exploring/blog"
SCRIPTS_DIR="${BLOG_DIR}/scripts"
LOG_DIR="${SCRIPTS_DIR}/logs"

mkdir -p "${LOG_DIR}"

# Ensure gh, git, and claude are discoverable
export PATH="/usr/bin:/usr/local/bin:/home/muklis/.local/bin:${PATH}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting blog generation run" >> "${LOG_DIR}/generation.log"

cd "${SCRIPTS_DIR}"
/usr/bin/python3 "${SCRIPTS_DIR}/generate_posts.py"
EXIT_CODE=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Run finished with exit code ${EXIT_CODE}" >> "${LOG_DIR}/generation.log"
exit ${EXIT_CODE}
