"""Join the daily sync cycle while the tab is on."""
from core import tab_api
from .service import school_service

api = tab_api.for_tab(__package__)


async def start():
    api.register_sync("School (Canvas)", school_service.sync)
