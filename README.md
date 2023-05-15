# Packstack API
Packstack API server

### Run local database
- `cd local-db`
- `docker-compose up --build`

### Build local docker image
`docker build -t packstack-api:latest .`

### Run local docker image
`docker run -p 80:80 --env-file=.local.env packstack-api:latest`

### Run production docker image
`docker run -p 80:80 --env-file=.prod.env packstack-api:latest`