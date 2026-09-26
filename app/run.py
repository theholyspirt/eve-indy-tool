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
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    host = os.getenv("FLASK_HOST", "localhost")
    # Fail loudly rather than silently expose Werkzeug's interactive, code-executing
    # debugger console to the network — it must only ever be reachable from the same
    # machine, never combined with a LAN/public-facing bind address.
    if debug and host not in ("localhost", "127.0.0.1"):
        raise RuntimeError(
            f"Refusing to start: FLASK_DEBUG=true with FLASK_HOST={host!r} would "
            "expose the interactive debugger to the network. Set FLASK_DEBUG=false "
            "for any host other than localhost/127.0.0.1."
        )
    app.run(host=host, debug=debug)
