# https://github.com/tiangolo/uvicorn-gunicorn-fastapi-docker
FROM tiangolo/uvicorn-gunicorn-fastapi:python3.8
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt
COPY ./app /app
