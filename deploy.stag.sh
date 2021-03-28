#!/usr/bin/env bash

doctl registry login
docker build -f stag.Dockerfile -t packstack .
docker tag packstack:latest registry.digitalocean.com/packstack/packstack-api
docker push registry.digitalocean.com/packstack/packstack-api