"""意向宝 MVP 后端应用包。"""


def create_app(*args, **kwargs):
    """Load the FastAPI factory lazily.

    Importing a utility such as ``app.filesystem_security`` must not create the
    default database, seed accounts or touch the real data directory as a side
    effect.  Uvicorn still imports ``app.main:app`` explicitly for startup.
    """

    from .main import create_app as factory

    return factory(*args, **kwargs)

__all__ = ["create_app"]
