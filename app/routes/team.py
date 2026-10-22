"""A non-admin view of who else uses the app.

It exists so that ordinary features -- an assignee dropdown, an owner picker,
an @mention list -- do not need admin rights just to render a list of names.
It returns id and display name only; role and disabled status stay behind the
admin endpoint, because those are facts about who administers the app rather
than about who you can hand work to.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.models import users
from app.routes.helpers import get_store, run


async def list_team(request: Request) -> Response:
    members = await run(users.list_users, get_store(request))
    return JSONResponse([{"id": user.id, "display_name": user.display_name} for user in members if not user.disabled])
