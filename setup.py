""" setup """
from setuptools import find_packages, setup

setup(
    name="sketched_nl2sql",
    version="0.0.1",
    package_dir={"": "src"},
    packages=find_packages("src"),
    # zip_safe=False,
)
