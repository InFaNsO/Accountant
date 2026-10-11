import os
from pathlib import Path

# Load .env file if it exists (production server sets env vars this way)
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Ledger running at http://127.0.0.1:{port}\n")
    app.run(debug=True, port=port)
