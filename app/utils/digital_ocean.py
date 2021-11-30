import os
import boto3
from botocore.exceptions import ClientError
from consts import DO_REGION, DO_SPACES_KEY, DO_SPACES_SECRET_KEY, DO_BUCKET

s3_client = boto3.client(
    's3',
    endpoint_url='https://nyc3.digitaloceanspaces.com',
    aws_access_key_id=DO_SPACES_KEY,
    aws_secret_access_key=DO_SPACES_SECRET_KEY,
    region_name=DO_REGION
)


def s3_file_upload(file, content_type, key, bucket=DO_BUCKET):
    """Upload a file to an Digital Ocean bucket

    :param file: FastAPI File object
    :param content_type: String denoting file content type
    :param key: Path-like string defining object location
    :param bucket: Bucket to upload to
    :return: response if file was uploaded, else False
    """

    try:
        s3_client.upload_fileobj(file,
                                 bucket,
                                 key,
                                 ExtraArgs={'ACL': 'public-read', 'ContentType': content_type})
    except ClientError as e:
        print(e)
        return False

    return True


def s3_file_delete(key):
    try:
        s3_client.delete_object(Bucket=DO_BUCKET, Key=key)
    except ClientError as e:
        print(e)
        return False

    return True
