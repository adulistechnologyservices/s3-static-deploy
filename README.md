# s3-static-deploy

Deploy a static site to S3 + CloudFront by uploading **only what changed**, then **prove** the deploy at the origin and at the edge.

One file, Python 3.9+, `boto3`. The planning and verification logic is pure Python with stdlib tests that need no AWS account.

## Why

A static site of 1,200 folders and 28,000 files was being redeployed with one `aws s3 cp` per file: 70 to 100 minutes for a change that touched eight files per folder. Three facts make that unnecessary:

1. **S3 already fingerprints every object.** For a single-part upload the ETag *is* the MD5 of the bytes. One paginated listing tells you exactly which local files differ from what is live. The same change now takes about ten minutes, most of it the verification.
2. **S3 sets no metadata for you.** Content-Type and Cache-Control are explicit on every upload here. A bare `text/html` without a charset is a real bug on any page with a non-ASCII character, and `aws s3 sync` will not repair metadata on an object whose bytes already match.
3. **"Invalidation: Completed" is a job status, not proof.** A mid-path wildcard like `/*.html` matches nothing and still completes. A second CloudFront distribution in front of the same bucket keeps serving the old bytes until it is invalidated too. So the tool refuses mid-path wildcards, invalidates every distribution you name, and then fetches representative files from every edge host and compares bytes with the build.

## Usage

```bash
pip install boto3
python3 s3deploy.py --dir build/ --bucket my-site-bucket \
  --distribution E1ABCDEF2GHIJ --distribution E2KLMNOP3QRST \
  --edge https://d111111abcdef8.cloudfront.net --edge https://www.example.com
```

Folder-per-site bucket layouts: add `--prefix sites/acme` and point `--edge` at that folder's URL.

`--dry-run` lists what would be uploaded, by category, and stops. It is also the cheapest answer to "is what is live the same as this build?"

`--force` re-uploads files whose bytes already match, which is how you repair a wrong Content-Type.

Exit status is non-zero if any upload failed, any file's ETag still differs after the upload, or any edge host serves different bytes.

## What it does not do

- Delete anything. Removing objects is a separate, deliberate step.
- Detect changes on objects whose ETag is not an MD5: multipart uploads (anything your client split, typically over 8 MB) and SSE-KMS encrypted objects. Those always look changed and are re-uploaded, which is safe but slow.
- Decide your TTLs for you. Pages, feeds and text files get `max-age=300`; everything else is treated as fingerprinted and gets a year with `immutable`. Edit `SHORT_LIVED` and `CONTENT_TYPES` for your site.

## Tests

```bash
python3 -m unittest -v
```

## Licence

MIT. Built and used in production by [ATS — Adulis Technology Services](https://adulistechnologyservices.com).
