"""Passive capture, background scans and owned calendar deadlines."""
from core import tab_api
from . import scanner
from .service import crm_service

api = tab_api.for_tab(__package__)


async def start():
    scanner.start()


async def stop():
    await scanner.stop()


def on_message(source, message):
    scanner.capture_connector(source["kind"], source["connection_id"], message)


def calendar_items(user, start, end):
    return crm_service.calendar_events(user, start, end)
