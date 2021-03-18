import os
import boto3
from botocore.exceptions import ClientError

default_bucket = os.getenv('S3_BUCKET')

s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY'),
    aws_secret_access_key=os.getenv('AWS_SECRET_KEY'),
    region_name=os.getenv('S3_BUCKET_REGION')
)


def s3_file_upload(file, content_type, key, bucket=default_bucket):
    """Upload a file to an S3 bucket

    :param file: FastAPI File object
    :param key: Path-like string defining object location
    :param bucket: Bucket to upload to
    :return: response if file was uploaded, else False
    """

    # todo set image content-type in ExtraArgs
    # https://stackoverflow.com/questions/4751360/amazon-s3-image-downloading-instead-of-displaying-in-browser
    # https://fastapi.tiangolo.com/tutorial/request-files/

    try:
        s3_client.upload_fileobj(file.file,
                                 bucket,
                                 key,
                                 ExtraArgs={'ACL': 'public-read', 'ContentType': content_type})
    except ClientError as e:
        print(e)
        return False

    return True
