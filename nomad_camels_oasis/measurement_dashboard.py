"""Dashboard plugin mounted inside NOMAD's dashboard catalogue."""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse


def create_dashboard() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    def index():
        # GUI v2 already embeds a dashboard in an iframe. Redirecting lets it
        # load the interactive setup page directly, instead of nesting a second
        # iframe that can interfere with focus and button interactions.
        return RedirectResponse(url="/nomad-oasis/camels/setup", status_code=307)

    return app
