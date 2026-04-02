#!/bin/bash
# AI Legal Assistant — Start Server
# Usage: ./start.sh
# Or:    ANTHROPIC_API_KEY=sk-ant-... ./start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if it exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs 2>/dev/null)
    echo "  Loaded .env file"
fi

echo ""
echo "  AI Legal Assistant"
echo "  ────────────────────"

# Kill any existing server on port 8080
lsof -ti:${PORT:-8080} 2>/dev/null | xargs kill -9 2>/dev/null || true

# Start the server
python3 "$SCRIPT_DIR/backend/server.py"
