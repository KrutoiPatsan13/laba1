from ..service_kit import run
from ..settings import require_secret, settings
from .app import app

require_secret("NETGUARD_JWT_SECRET", settings.jwt_secret)
run(app, settings.reporting_port, settings.bind_host)
