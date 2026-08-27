#!/bin/sh

set -eu

MINIO_ALIAS=local
MINIO_URL=http://minio:9000
POLICY_NAME=vision-artifact-writer
POLICY_FILE=/tmp/vision-artifact-policy.json

while ! mc alias set "$MINIO_ALIAS" "$MINIO_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
    echo "Waiting for MinIO..."
    sleep 2
done

while ! mc mb --ignore-existing "$MINIO_ALIAS/$VISION_ARTIFACT_BUCKET" >/dev/null 2>&1; do
    echo "Waiting for MinIO bucket API..."
    sleep 2
done

cat >"$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads"
      ],
      "Resource": ["arn:aws:s3:::${VISION_ARTIFACT_BUCKET}"]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": ["arn:aws:s3:::${VISION_ARTIFACT_BUCKET}/*"]
    }
  ]
}
EOF

if ! mc admin policy info "$MINIO_ALIAS" "$POLICY_NAME" >/dev/null 2>&1; then
    mc admin policy create "$MINIO_ALIAS" "$POLICY_NAME" "$POLICY_FILE"
fi

if ! mc admin user info "$MINIO_ALIAS" "$VISION_MINIO_ACCESS_KEY" >/dev/null 2>&1; then
    mc admin user add \
        "$MINIO_ALIAS" \
        "$VISION_MINIO_ACCESS_KEY" \
        "$VISION_MINIO_SECRET_KEY"
fi

mc admin policy attach \
    "$MINIO_ALIAS" \
    "$POLICY_NAME" \
    --user "$VISION_MINIO_ACCESS_KEY"

echo "MinIO ready: bucket=$VISION_ARTIFACT_BUCKET app_user=$VISION_MINIO_ACCESS_KEY"
