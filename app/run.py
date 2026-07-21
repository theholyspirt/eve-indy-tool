import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    # Defaults to off — Flask's debug mode exposes an interactive, code-executing
    # console on crashes, which must never be reachable outside local dev.
    # Set FLASK_DEBUG=true in .env to enable auto-reload + the debugger locally.
    # Bind by hostname (not 127.0.0.1) so the printed URL matches EVE_CALLBACK_URL
    # in .env — EVE SSO's redirect and the browser's session cookie both need the
    # same host you started the login flow from.
    app.run(host="localhost", debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
