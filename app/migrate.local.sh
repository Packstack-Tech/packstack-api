#!/bin/bash

ECHO "Applying migration to local database"

export PYTHONPATH=$(pwd)
export DATABASE_URL=postgresql://root:password@localhost/packstack

ECHO "Enter migration commit message: "
read commit_msg

alembic revision --autogenerate -m $commit_msg
alembic upgrade head