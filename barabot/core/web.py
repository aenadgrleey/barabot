"""The one HTTP server behind PUBLIC_URL, for every domain that serves web pages or APIs.

`__main__` builds each domain's Starlette app and hands them over with the
path they live under; this module knows nothing about the domains:

    /app/...   the Mini App
    /...       the MCP connector, which needs the root for /.well-known OAuth metadata

Mounts are tried in order, so the root mount goes last. Starlette does not
run the lifespan of a mounted app, so `WebService` runs each one itself;
the MCP app starts its session manager there.

`WebService` runs uvicorn on 127.0.0.1:WEB_PORT; an HTTPS proxy in front
(Tailscale Funnel on the server) publishes it as PUBLIC_URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator, Sequence

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from barabot.core.config import WebConfig

log = logging.getLogger(__name__)


class _QuietServer(uvicorn.Server):
    """Leave SIGINT and SIGTERM to barabot's own handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def compose(mounts: Sequence[tuple[str, Starlette]]) -> Starlette:
    """One app from (path, app) pairs. A path is "" for the root or "/name" for a prefix."""
    routes: list[BaseRoute] = []
    for path, app in mounts:
        if path:
            # /app goes to /app/, so relative URLs in the page resolve under the prefix.
            async def slash(request: Request, path: str = path) -> Response:
                return RedirectResponse(path + "/", status_code=308)

            routes.append(Route(path, slash))
        routes.append(Mount(path, app=app))

    @contextlib.asynccontextmanager
    async def lifespan(parent: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for _path, app in mounts:
                await stack.enter_async_context(app.router.lifespan_context(app))
            yield

    return Starlette(routes=routes, lifespan=lifespan)


class WebService:
    def __init__(self, cfg: WebConfig, mounts: Sequence[tuple[str, Starlette]]) -> None:
        self.cfg = cfg
        self.app = compose(mounts)
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=cfg.port, log_level="warning", proxy_headers=True, lifespan="on"
        )
        self._server = _QuietServer(config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(), name="web")
        while not self._server.started:
            if self._task.done():
                self._task.result()  # raises the bind or startup error
                raise RuntimeError("Web server stopped during startup")
            await asyncio.sleep(0.05)
        log.info("Web server on 127.0.0.1:%d as %s", self.cfg.port, self.cfg.public_url)

    async def run_forever(self, stop: asyncio.Event) -> None:
        assert self._task is not None, "start() runs first"
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((stopping, self._task), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not stop.is_set():
            self._task.result()
            raise RuntimeError("Web server stopped")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        with contextlib.suppress(Exception):
            await self._task
