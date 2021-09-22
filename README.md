# Packstack API
Packstack API server

### Run server locally
- `docker-compose up --build`

### Apply a migration locally
- Make sure the database is up and available
- From `/app`, run `. ./migrate.local.sh`
- Enter commit message and hit enter
- NOTE: You may need to `pip install -r requirements.txt`

