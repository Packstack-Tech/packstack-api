# 
FROM python:3.9.12

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

#
ENV PYTHONPATH "${PYTHONPATH}:/code/app"

# 
CMD ["uvicorn", "app.main:app", "--proxy-headers", "--host", "0.0.0.0", "--port", "80"]
