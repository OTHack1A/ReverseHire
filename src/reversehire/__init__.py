"""ReverseHire: from a CV to the matching companies that are hiring."""

from ._version import BUILD_DATE, __version__  # the one place that holds the version

# The developer's name: the first character is the digit zero.
DEVELOPER = "0THack1A"
# The public repository. The GitHub account is currently spelled with the letter O
# instead of the digit zero, unlike the developer's name: change it here only (the
# single place in the code that holds this address; docs/PUBLISHING.md lists the files).
REPOSITORY_URL = "https://github.com/OTHack1A/ReverseHire"


def build_number(version: str = __version__) -> int:
    """The build number: the third number of the version (it grows at every build)."""
    parts = version.split(".")
    try:
        return int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return 0


def version_text(version: str = __version__, build_date: str = BUILD_DATE) -> str:
    """"0.1.7 (2026-11-18)": the version and the date of its build (version alone
    when the checkout was never built)."""
    return f"{version} ({build_date})" if build_date else version
