from ..service_kit import run
from ..settings import settings
from .app import app

run(app, settings.reporting_port, settings.bind_host)
