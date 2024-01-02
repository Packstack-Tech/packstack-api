# Packstack API

Salve! You've reached Packstack's API services.

I've tried to making running this locally as simple as possible. You'll need [Docker installed](https://docs.docker.com/engine/install/).

I'd also suggest you give [TablePlus](https://tableplus.com/) a try if you don't already have a preferred database GUI.

When you're ready, from the project root, run `docker-compose up --build`.

This will run the FastAPI service and database service in a container. The API will be exposed at `localhost`.

To verify the service is running, go to `http://localhost/health-check` and you should see the message "Packstack API is available".

## Notes

- FastAPI automatically generates [Swagger Docs](http://localhost/docs) for reference
- Hit `GET /resources/seed` to seed Brands and Categories with initial data
- Third-party services, like email, are disabled in local development
- There are currently endpoints that aren't in use and hitting them may yield unexpected results
- The frontend app, which is run separately, [can be found here](https://github.com/Packstack-Tech/app)
