import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Ledger running at http://127.0.0.1:{port}\n")
    app.run(debug=True, port=port)
