# https://github.com/tiangolo/uvicorn-gunicorn-fastapi-docker
FROM tiangolo/uvicorn-gunicorn-fastapi:python3.8
WORKDIR /app

ENV DATABASE_URL postgresql://doadmin:db8hb24b8c052bhi@private-packstack-db-staging-do-user-8976642-0.b.db.ondigitalocean.com:25060/defaultdb
ENV POSTGRES_USER: doadmin
ENV POSTGRES_PASSWORD: db8hb24b8c052bhi
ENV POSTGRES_HOST: private-packstack-db-staging-do-user-8976642-0.b.db.ondigitalocean.com
ENV POSTGRES_DB: defaultdb
ENV PORT: 80
ENV JWT_SECRET: 0839942409BCFFF12C92AF8C66BF5BD06086F423C361D7C36BA8CB2CFBFCD8EF
ENV JWT_ALGORITHM: HS256

ENV DO_API_KEY d8a87c88e9e65d3868e30eb862d57118ebba0ae7753d870114a34a21db161e50
ENV AWS_ACCESS_KEY: AKIA46VHIVDUITVUG2CV
ENV AWS_SECRET_KEY: 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
ENV S3_BUCKET_REGION: us-east-2
ENV S3_BUCKET: packstack-user-images-dev

COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt
COPY ./app /app
