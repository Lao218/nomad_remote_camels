"""Dashboard plugin mounted inside NOMAD's dashboard catalogue."""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse


def create_dashboard() -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return """<!doctype html><html><head><title>CAMELS Measurements</title>
        <style>body{font-family:Arial,sans-serif;margin:0}header{padding:1rem 1.5rem;border-bottom:1px solid #ddd}iframe{width:100%;height:calc(100vh - 76px);border:0}</style>
        </head><body><header><strong>CAMELS Measurements</strong> — configure a protocol and start a measurement.</header>
        <iframe src=\"/nomad-oasis/camels/dashboard\" title=\"CAMELS measurement dashboard\"></iframe>
        </body></html>"""

    return app
