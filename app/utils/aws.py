import os
import boto3
from botocore.exceptions import ClientError

s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY'),
    aws_secret_access_key=os.getenv('AWS_SECRET_KEY'),
    region_name="us-east-2"
)

default_bucket = 'packstack-user-images'


def s3_file_upload(file, user_id, bucket=default_bucket):
    """Upload a file to an S3 bucket

    :param file: FastAPI File object
    :param user_id: Associated user id
    :param bucket: Bucket to upload to
    :return: response if file was uploaded, else False
    """

    # todo figure out how to get resource url once uploaded
    # todo folder structure plus public permissions

    # Upload the file
    try:
        response = s3_client.upload_fileobj(file.file,
                                            bucket,
                                            str(user_id) + '/' + file.filename,
                                            ExtraArgs={'Metadata': {'userId': str(user_id)}})
    except ClientError as e:
        print(e)
        return False

    print(response)

    return response
