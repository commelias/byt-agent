"""Сервис «Быт»: MCP-сервер журнала + планировщик напоминаний в одном процессе."""
import contextlib
import logging

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from app import config, db, scheduler
from app.mcp_server import mcp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("byt")


class BearerAuth(BaseHTTPMiddleware):
    """Всё под /mcp закрыто токеном MCP_TOKEN. Без него любой, кто узнал адрес, читал бы журнал."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            if not config.MCP_TOKEN:
                return JSONResponse({"error": "MCP_TOKEN не задан в переменных окружения"}, status_code=500)
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {config.MCP_TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_: Request):
    return PlainTextResponse("ok")


async def diag(request: Request):
    """Диагностика связи с Telegram; закрыта тем же MCP_TOKEN: /diag?token=..."""
    from app import telegram
    if not config.MCP_TOKEN or request.query_params.get("token") != config.MCP_TOKEN:
        return PlainTextResponse("unauthorized", status_code=401)
    lines = [await telegram.diagnose(), "", "Последние доставки:"]
    lines += [f"{d['ts']} {d['kind']} {'ok' if d['ok'] else 'СБОЙ'} {d['detail'] or ''}" for d in db.deliveries(limit=20)]
    lines += ["", "Разовые напоминания в очереди:"]
    lines += [f"#{r['id']} {r['at']} {r['text']}" for r in db.pending_reminders()] or ["нет"]
    return PlainTextResponse("\n".join(lines))


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    db.init()
    sched = scheduler.start()
    async with mcp.session_manager.run():
        yield
    sched.shutdown(wait=False)


app = Starlette(
    routes=[
        Route("/", health),
        Route("/health", health),
        Route("/diag", diag),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    middleware=[Middleware(BearerAuth)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
