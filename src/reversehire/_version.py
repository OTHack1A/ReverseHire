"""The version of ReverseHire: the single source of truth.

MAJOR.MINOR.PATCH. The PATCH number is the build number: `tools/build.py` adds one
at every build and rewrites this file (pyproject.toml reads the version from here:
`[tool.setuptools.dynamic]`). BUILD_DATE is the ISO date of that build; "" in a
source checkout that has not been built since the version was set.
Keep both as plain string assignments: setuptools and tools/build.py read them
without importing the package.
"""

__version__ = "0.1.4"
BUILD_DATE = "2026-11-19"