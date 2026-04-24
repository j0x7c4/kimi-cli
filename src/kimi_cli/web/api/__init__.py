"""API routes."""

from kimi_cli.web.api import admin as admin_module
from kimi_cli.web.api import auth_user, config, open_in, sessions

config_router = config.router
sessions_router = sessions.router
work_dirs_router = sessions.work_dirs_router
open_in_router = open_in.router
auth_router = auth_user.router
admin_router = admin_module.router

__all__ = [
    "admin_router",
    "auth_router",
    "config_router",
    "open_in_router",
    "sessions_router",
    "work_dirs_router",
]
