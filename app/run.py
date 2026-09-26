import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    # Defaults to off — Flask's debug mode exposes an interactive, code-executing
    # console on crashes, which must never be reachable outside local dev.
    # Set FLASK_DEBUG=true in .env to enable auto-reload + the debugger locally.
    # Bind address is configurable via .env (gitignored) so a real deployment can
    # set FLASK_HOST=0.0.0.0 to accept LAN connections, while local dev keeps the
    # "localhost" default — without this being a per-deployment code edit that a
    # future `git pull` would silently revert.
    app.run(host=os.getenv("FLASK_HOST", "localhost"), debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
