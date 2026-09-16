# 
FROM python:3.12-slim

# 
WORKDIR /code

# 
COPY ./requirements.txt /code/requirements.txt

#
RUN python -m pip install --upgrade pip

# 
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# 
COPY ./app /code/app
COPY ./models /code/models

# /code/app for the FastAPI package, /code so `from models.base import …`
# resolves to the in-repo models package (formerly a separate git dependency).
ENV PYTHONPATH "${PYTHONPATH}:/code/app:/code"

# 
CMD ["uvicorn", "app.main:app", "--proxy-headers", "--host", "0.0.0.0", "--port", "80"]
