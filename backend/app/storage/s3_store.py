"""S3-compatible object store for AWS S3 and local MinIO."""


class S3ObjectStore:
    def __init__(self, bucket: str, endpoint: str = "", access_key: str = "", secret_key: str = ""):
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise RuntimeError("Install boto3 to use S3 or MinIO object storage") from exc
        self._client_error = ClientError
        self.bucket = bucket
        self.client = boto3.client(
            "s3", endpoint_url=endpoint or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
        )
        try:
            self.client.head_bucket(Bucket=bucket)
        except ClientError:
            self.client.create_bucket(Bucket=bucket)

    def key_for(self, object_hash: str) -> str:
        return f"objects/{object_hash[:2]}/{object_hash[2:4]}/{object_hash}"

    def exists(self, object_hash: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key_for(object_hash))
            return True
        except self._client_error as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def put(self, object_hash: str, payload: bytes) -> bool:
        if self.exists(object_hash):
            return False
        self.client.put_object(
            Bucket=self.bucket, Key=self.key_for(object_hash), Body=payload,
            ContentType="application/octet-stream",
            Metadata={"content-hash": object_hash},
        )
        return True

    def get(self, object_hash: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self.key_for(object_hash))["Body"].read()

    def delete(self, object_hash: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.key_for(object_hash))

    def size(self, object_hash: str) -> int:
        return int(self.client.head_object(Bucket=self.bucket, Key=self.key_for(object_hash))["ContentLength"])
