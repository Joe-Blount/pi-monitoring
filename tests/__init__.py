"""Makes the tests a package.

Without this the suite collects only when pytest happens to put the working
directory on the import path, so `pytest` run from anywhere else fails, and
the registry entries that name "tests.test_runner" resolve to a second copy of
the module rather than the one the test is running in.
"""
