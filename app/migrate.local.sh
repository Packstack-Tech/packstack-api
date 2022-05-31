#!/bin/bash

ECHO "Applying migration to local database"

export PYTHONPATH=$(pwd)
export POSTGRES_USER=root
export POSTGRES_PASSWORD=password
export POSTGRES_HOST=localhost
export POSTGRES_DB=packstack

ECHO "Enter migration commit message: "
read commit_msg

alembic revision --autogenerate -m $commit_msg
alembic upgrade head