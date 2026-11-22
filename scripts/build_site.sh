#!/usr/bin/env bash
# Build dist/: the static site, vendored MapLibre, and the exported cell/district data.
# Runs locally, in CI, and in Cloudflare Workers Builds (`npm run build`).
set -euo pipefail
cd "$(dirname "$0")/.."

# Workers Builds starts from a bare image: make sure the Python environment exists.
uv sync --frozen --no-dev

rm -rf dist
mkdir -p dist/vendor dist/data
cp site/index.html site/app.js site/style.css site/_headers dist/
cp node_modules/maplibre-gl/dist/maplibre-gl.css dist/vendor/
cp node_modules/maplibre-gl/dist/maplibre-gl.mjs node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs \
   node_modules/maplibre-gl/dist/maplibre-gl-shared.mjs dist/vendor/
uv run fiberyet --data-dir "${DATA_DIR:-data}" export --out dist/data
