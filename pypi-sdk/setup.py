from setuptools import setup

# pyproject.toml carries all metadata (PEP 621). setup.py stays as a
# minimal shim so legacy tooling / `python setup.py install` still works.
setup(
    py_modules=["linkpeek"],
)
