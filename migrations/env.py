from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import core as _core_models  # noqa: F401
from sreda.db.models import eds_monitor as _eds_models  # noqa: F401
from sreda.db.models import skill_platform as _skill_platform_models  # noqa: F401
from sreda.db.models import user_profile as _user_profile_models  # noqa: F401
from sreda.db.models import memory as _memory_models  # noqa: F401
from sreda.db.models import inbound_event as _inbound_event_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata

# Explicit naming convention for implicit FK/UQ/IX/PK constraints.
# Alembic >= 1.18 refuses to recreate SQLite tables in batch mode when
# any existing constraint is unnamed, which breaks incremental
# ``batch_alter_table`` migrations. Declaring the convention here
# makes both new migrations and ``render_as_batch`` round-trips name
# their constraints consistently.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        naming_convention=NAMING_CONVENTION,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            naming_convention=NAMING_CONVENTION,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
