import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


class S3Storage:
    """S3-compatible object storage (AWS S3 / Cloudflare R2 / MinIO).

    `endpoint_url` is what switches targets: unset for AWS, the R2 account
    endpoint for R2, `http://localhost:9000` for a local MinIO.
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # SigV4 + virtual-addressing-off keeps R2 and MinIO happy.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def presigned_put_url(self, key: str, content_type: str, expires_in: int) -> str:
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )

    def presigned_get_url(self, key: str, expires_in: int) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    def object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
