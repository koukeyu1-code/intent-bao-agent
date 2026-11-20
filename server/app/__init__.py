"""意向宝 MVP 后端应用包。"""


def create_app(*args, **kwargs):
    """Load the FastAPI factory lazily.

    Importing a utility such as ``app.filesystem_security`` must not create the
    default database, seed accounts or touch the real data directory as a side
    effect.  Startup uses the generated entry file that calls this factory with
    an explicit data directory (see scripts/start-local.*).
    """

    from .main import create_app as factory

    return factory(*args, **kwargs)

__all__ = ["create_app"]
