#!/usr/bin/env python3
"""
Standalone AWS authentication test.

Loads access keys from .env (same vars as modules/utils/config.py and
s3_to_sharepoint.py: AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, falling back to
the legacy AWS_ACCESS_KEY/AWS_SECRET_KEY names) and confirms they can
authenticate against AWS and reach the reporting S3 bucket.

Usage:
    python3 test_aws_auth.py
    python3 test_aws_auth.py --env-file /path/to/.env
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

S3_BUCKET = "produk-rdsextracts-438255373632"


def load_env_file(path: Path):
    """Load .env by sourcing it in bash, matching how cron_wrapper.sh jobs do it
    (`set -a && source .env && set +a`) - handles `export `, quoting, and
    variable references that a naive KEY=VALUE line parser would miss."""
    if not path.exists():
        print(f"✗ .env file not found: {path}")
        sys.exit(1)

    marker = "__ENV_AFTER_SOURCE__"
    script = f"set -a && source {path} && set +a && echo {marker} && env -0"
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=False
    )
    if result.returncode != 0:
        print(f"✗ Failed to source {path}: {result.stderr.decode(errors='replace')}")
        sys.exit(1)

    output = result.stdout.decode(errors="replace")
    marker_pos = output.find(marker)
    if marker_pos == -1:
        print(f"✗ Could not parse environment after sourcing {path}")
        sys.exit(1)
    env_blob = output[marker_pos + len(marker):].lstrip("\n")

    for entry in env_blob.split("\0"):
        if "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        os.environ[key] = value

    print(f"✓ Loaded environment from {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default="/s3reporting/.env",
        help="Path to the .env file (default: /s3reporting/.env)",
    )
    args = parser.parse_args()

    load_env_file(Path(args.env_file))

    access_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

    if not access_key or not secret_key:
        print("✗ AWS credentials not found in .env (need AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY)")
        sys.exit(1)

    print(f"✓ Found access key: {access_key[:4]}...{access_key[-4:]}")

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        print("✗ boto3 is not installed (pip install boto3)")
        sys.exit(1)

    session_kwargs = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
    }
    if region:
        session_kwargs["region_name"] = region

    sts_client = boto3.client("sts", **session_kwargs)

    try:
        identity = sts_client.get_caller_identity()
        print("✓ AWS authentication succeeded")
        print(f"  Account: {identity['Account']}")
        print(f"  ARN:     {identity['Arn']}")
        print(f"  UserId:  {identity['UserId']}")
    except NoCredentialsError:
        print("✗ AWS authentication failed: no credentials provided")
        sys.exit(1)
    except ClientError as e:
        print(f"✗ AWS authentication failed: {e}")
        sys.exit(1)

    # Confirm the credentials can actually reach the reporting bucket. Uses
    # list_objects_v2 (same call s3_to_sharepoint.py's list_s3_objects() makes)
    # rather than head_bucket: HeadBucket requires s3:ListBucket scoped to the
    # bucket ARN itself, which some IAM policies grant differently than
    # s3:ListBucket/s3:GetObject scoped to objects - a 403 here doesn't
    # necessarily mean the real sync jobs would fail too.
    s3_client = boto3.client("s3", **session_kwargs)
    try:
        s3_client.list_objects_v2(Bucket=S3_BUCKET, MaxKeys=1)
        print(f"✓ S3 access confirmed for bucket: {S3_BUCKET}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        print(f"✗ S3 access check failed for bucket {S3_BUCKET}: {code} - {e}")
        sys.exit(1)

    print("\n✓ All AWS authentication checks passed!")


if __name__ == "__main__":
    main()
