from .app import create_app
from .config import Settings
from .postgres import PostgresStore


settings = Settings.from_environment()
app = create_app(settings, PostgresStore(settings.database_url))
