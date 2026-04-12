#!/bin/bash
# Initialize LocalStack S3 bucket for artifact storage.
# This script runs automatically when LocalStack reaches the "ready" state
# via the /etc/localstack/init/ready.d/ mount.

set -euo pipefail

echo "Creating platform-artifacts S3 bucket..."
awslocal s3 mb s3://platform-artifacts 2>/dev/null || true
echo "LocalStack S3 initialization complete."
awslocal s3 ls
