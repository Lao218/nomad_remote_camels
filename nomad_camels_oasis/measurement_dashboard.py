"""Dashboard plugin mounted inside NOMAD's dashboard catalogue."""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse


def _redirect_dashboard(target: str) -> FastAPI:
    app = FastAPI()

    @app.get("/")
    def index():
        # GUI v2 already embeds a dashboard in an iframe. Redirecting lets it
        # load the interactive setup page directly, instead of nesting a second
        # iframe that can interfere with focus and button interactions.
        return RedirectResponse(url=target, status_code=307)

    return app


def create_dashboard() -> FastAPI:
    """Connection-management dashboard kept for backwards compatibility."""
    return _redirect_dashboard("/nomad-oasis/camels/setup")


def create_monitor_dashboard() -> FastAPI:
    """Dedicated dashboard for measurement status and live plots."""
    return _redirect_dashboard("/nomad-oasis/camels/dashboard")
