"""S3 storage with server-side encryption under a customer-managed KMS key (SSE-KMS)."""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from app.storage.base import StorageBackend, StorageError


class S3Storage(StorageBackend):
    name = "s3"

    def __init__(
        self,
        bucket: str,
        kms_key_id: str,
        *,
        region: str,
        prefix: str = "",
        endpoint_url: str = "",
        client=None,
    ) -> None:
        if not bucket or not kms_key_id:
            raise StorageError("S3 storage needs a bucket and a KMS key id")
        self.bucket = bucket
        self.kms_key_id = kms_key_id
        self.prefix = prefix
        self.client = client or boto3.client(
            "s3", region_name=region, endpoint_url=endpoint_url or None
        )

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(key),
                Body=data,
                ContentType="application/octet-stream",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self.kms_key_id,
                BucketKeyEnabled=True,
                Metadata={"original-content-type": content_type},
            )
        except ClientError as exc:
            raise StorageError(f"S3 put failed: {exc.response['Error'].get('Code')}") from exc

    def get(self, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            raise StorageError(f"S3 get failed: {exc.response['Error'].get('Code')}") from exc
        return obj["Body"].read()

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            raise StorageError(f"S3 delete failed: {exc.response['Error'].get('Code')}") from exc
