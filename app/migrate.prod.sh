#!/bin/bash

ECHO "Applying migration to production database"

export PYTHONPATH=$(pwd)

# ECHO "Enter migration commit message: "
# read commit_msg

# may need to stamp specific revision and upgrade from there if out of sync
# likely will not need to create a revision here; handled locally

# alembic --name production revision --autogenerate -m $commit_msg
alembic --name production upgrade head