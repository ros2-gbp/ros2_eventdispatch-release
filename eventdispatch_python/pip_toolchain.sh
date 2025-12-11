#!/bin/bash

# run in this directory

# update
# pyproject.toml
# setup.py

# (base) ubuntu@ubuntu24dev:/home/charlieyan1/Dev/jim/ros2_eventdispatch$ pip list | grep twine
# twine                    6.2.0
# (base) ubuntu@ubuntu24dev:/home/charlieyan1/Dev/jim/ros2_eventdispatch$ pip list | grep build
# build                    0.9.0

rm -rf dist/
python3 -m build
twine upload dist/*

