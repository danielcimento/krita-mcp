"""Plugin configuration constants. Pure: no krita / PyQt5 imports.

Customize these as needed. (P1.4 replaces this with real config loading.)
"""

import os

SERVER_PORT = 5678
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")

# Directories that save/open_file may touch. Paths outside these are refused.
ALLOWED_DIRS = [CANVAS_OUTPUT_DIR]

# Shared secret; the MCP server reads the same file. Created on first run
# (by the extension at startup, not at import).
TOKEN_FILE = os.path.expanduser("~/.krita-mcp-token")
TOKEN_HEADER = "X-Krita-MCP-Token"

# Resource limits
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_CANVAS_DIM = 8192
MAX_CANVAS_PIXELS = 4096 * 4096
MAX_BRUSH_SIZE = 1000
MAX_STROKE_POINTS = 10000
MAX_LIMIT = 1000

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".kra", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".ora", ".psd"}
ALLOWED_HOSTS = {f"localhost:{SERVER_PORT}", f"127.0.0.1:{SERVER_PORT}"}

COORD = 10 * MAX_CANVAS_DIM  # generous bound for coordinates
