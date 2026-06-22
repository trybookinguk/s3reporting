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
import sys
from pathlib import Path

S3_BUCKET = "produk-rdsextracts-438255373632"


def load_env_file(path: Path):
    """Minimal .env loader (KEY=VALUE per line, '#' comments, no export)."""
    if not path.exists():
        print(f"✗ .env file not found: {path}")
        sys.exit(1)

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

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

    # Confirm the credentials can actually reach the reporting bucket.
    s3_client = boto3.client("s3", **session_kwargs)
    try:
        s3_client.head_bucket(Bucket=S3_BUCKET)
        print(f"✓ S3 access confirmed for bucket: {S3_BUCKET}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        print(f"✗ S3 access check failed for bucket {S3_BUCKET}: {code} - {e}")
        sys.exit(1)

    print("\n✓ All AWS authentication checks passed!")


if __name__ == "__main__":
    main()
