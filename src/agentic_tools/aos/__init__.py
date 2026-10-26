"""AOS component qualification version, separate from the toolbox distribution.

The agentic-tools project distribution retains its own release cadence. Neither
version replaces an exact candidate hash for qualification.
"""
from importlib.metadata import PackageNotFoundError, version

__version__ = "0.2.5"


def version_receipt() -> dict:
    try:
        distribution = version("agentic-tools")
    except PackageNotFoundError:
        distribution = None
    return {"aos_component_version": __version__, "package_version_scope": "AOS component",
            "distribution_name": "agentic-tools", "distribution_version": distribution}
