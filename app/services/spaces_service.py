import os
import boto3
from botocore.client import Config

DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")


def upload_image_to_spaces(file_bytes, filename, content_type):
    """Sube una imagen a DigitalOcean Spaces y retorna la URL pública."""
    session = boto3.session.Session()
    client = session.client(
        's3',
        region_name='sfo3',
        endpoint_url=DO_SPACES_ENDPOINT,
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET,
        config=Config(signature_version='s3v4')
    )
    client.put_object(Bucket=DO_SPACES_BUCKET, Key=filename, Body=file_bytes, ACL='public-read', ContentType=content_type)
    url = f"{DO_SPACES_ENDPOINT}/{filename}"
    return url
