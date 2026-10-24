"""Tiny S3 helper for the local eval: put/delete/head the eval route config.
Reads AWS credentials from admin_console/config.properties (never prints them)."""
import sys
from pathlib import Path
import boto3

from common import load_config

CFG = load_config()
BUCKET = CFG["s3_bucket"]
KEY = "conf/lb/eval/application.yml"
s3 = boto3.client(
    "s3",
    region_name=CFG.get("aws_region", "us-east-1"),
    aws_access_key_id=CFG["aws_access_key_id"],
    aws_secret_access_key=CFG["aws_secret_access_key"],
)

cmd = sys.argv[1] if len(sys.argv) > 1 else "head"
if cmd == "put":
    body = Path(sys.argv[2]).read_bytes()
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=body, ContentType="text/yaml")
    print("PUT s3://%s/%s" % (BUCKET, KEY))
elif cmd == "delete":
    s3.delete_object(Bucket=BUCKET, Key=KEY)
    print("DELETED s3://%s/%s" % (BUCKET, KEY))
elif cmd == "head":
    r = s3.head_object(Bucket=BUCKET, Key=KEY)
    print("ETag=%s size=%s" % (r["ETag"], r["ContentLength"]))
