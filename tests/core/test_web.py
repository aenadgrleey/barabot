"""The shared web server: domain apps under their paths, with their lifespans."""

import contextlib

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from barabot.core.web import compose


def app(name, events):
    @contextlib.asynccontextmanager
    async def lifespan(_):
        events.append(f"{name} up")
        yield
        events.append(f"{name} down")

    async def where(request):
        return PlainTextResponse(f"{name}:{request.url.path}")

    return Starlette(routes=[Route("/{rest:path}", where)], lifespan=lifespan)


def test_mounts_route_by_prefix_and_run_every_lifespan():
    events = []
    composed = compose([("/app", app("app", events)), ("", app("root", events))])
    with TestClient(composed, follow_redirects=False) as client:
        assert events == ["app up", "root up"]
        assert client.get("/app/x").text == "app:/app/x"
        assert client.get("/app").status_code == 308
        assert client.get("/mcp").text == "root:/mcp"
    assert events == ["app up", "root up", "root down", "app down"]
