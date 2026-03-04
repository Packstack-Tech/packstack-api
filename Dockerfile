# 
FROM python:3.12-slim

# 
WORKDIR /code

# 
COPY ./requirements.txt /code/requirements.txt

#
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --upgrade pip

# 
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# 
COPY ./app /code/app

#
ENV PYTHONPATH "${PYTHONPATH}:/code/app"

# 
CMD ["uvicorn", "app.main:app", "--proxy-headers", "--host", "0.0.0.0", "--port", "80"]
