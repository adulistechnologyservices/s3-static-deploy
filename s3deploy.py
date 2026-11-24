#!/usr/bin/env python3
"""Deploy a static site to S3 + CloudFront by uploading only what changed, then prove it.

    python3 s3deploy.py --dir build/ --bucket my-site-bucket \
        [--prefix sites/acme] [--distribution E1ABC... --distribution E2DEF...] \
        [--edge https://d111111abcdef8.cloudfront.net/sites/acme] [--workers 16] [--dry-run]

How it works
  1. List the bucket once. For an object uploaded in a single part, S3's ETag is the MD5 of
     its bytes, so the listing is a free fingerprint of everything that is live.
  2. Hash the local build. Keep only files whose MD5 differs from the live ETag, or which are
     not live at all. Nothing else is touched.
  3. Upload those with an explicit Content-Type and Cache-Control per extension (S3 guesses
     neither; "text/html" without a charset is a real bug on a page with non-ASCII text).
  4. Invalidate "/*" on EVERY distribution in front of the bucket. A mid-path wildcard such as
     "/*.html" matches nothing and still reports Completed; a second distribution serving the
     same bucket keeps serving the old bytes until it is invalidated too.
  5. Prove it twice: re-list the bucket and compare every file's ETag to the local MD5 (origin),
     then fetch a few representative files from each edge host and compare bytes (edge).
     "Invalidation: Completed" is a job status, not proof that the edge serves the new file.

Limits, stated rather than hidden: it deletes nothing; it cannot repair a wrong Content-Type on
an object whose bytes are already right (re-upload with --force for that); an object uploaded
as multipart or encrypted with SSE-KMS has an ETag that is not its MD5 and will always look
changed. Requires boto3; the planning and verification logic is pure Python and tested without AWS.
"""
import argparse, hashlib, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif", ".ico": "image/x-icon", ".woff2": "font/woff2",
    ".woff": "font/woff", ".pdf": "application/pdf",
}
# Pages and feeds change under the same name, so they get a short TTL. Everything else is
# expected to be fingerprinted in its filename or query string and can be cached for a year.
SHORT_LIVED = {".html", ".xml", ".txt", ".json"}
EDGE_SAMPLE = ("index.html", "styles.css", "script.js")


def md5_of(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def content_type(rel: str) -> str:
    return CONTENT_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def cache_control(rel: str) -> str:
    return "public, max-age=300" if Path(rel).suffix.lower() in SHORT_LIVED else "public, max-age=31536000, immutable"


def local_manifest(root: Path, workers: int = 8) -> dict:
    """relative posix path -> md5, for every file under root."""
    files = [p for p in sorted(root.rglob("*")) if p.is_file()]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        hashes = list(ex.map(md5_of, files))
    return {p.relative_to(root).as_posix(): h for p, h in zip(files, hashes)}


def plan(local: dict, live: dict, prefix: str = "", force: bool = False) -> dict:
    """Classify every local file: 'new' (not live), 'changed' (ETag differs), 'same'."""
    out = {"new": [], "changed": [], "same": []}
    for rel, h in local.items():
        key = f"{prefix}/{rel}" if prefix else rel
        if key not in live:
            out["new"].append(rel)
        elif live[key] != h or force:
            out["changed"].append(rel)
        else:
            out["same"].append(rel)
    return out


def invalidation_paths(paths=("/*",)) -> list:
    """CloudFront accepts a trailing wildcard only; refuse the silent kind."""
    for p in paths:
        if "*" in p[:-1]:
            raise ValueError(f"'{p}': a wildcard anywhere but the end matches nothing and still reports Completed")
    return list(paths)


def verify_origin(local: dict, live_after: dict, prefix: str = "") -> list:
    """Files whose live ETag still differs from the local MD5 after the upload."""
    return [rel for rel, h in local.items() if live_after.get(f"{prefix}/{rel}" if prefix else rel) != h]


def verify_edge(local: dict, fetch, edges, sample=EDGE_SAMPLE) -> list:
    """(edge, rel, 'differs'|'network') for each sampled file an edge host does not serve byte-exact.
    `fetch(url)` returns bytes or None; injected so the check is testable without a network."""
    bad = []
    for edge in edges:
        for rel in sample:
            if rel not in local:
                continue
            got = fetch(f"{edge.rstrip('/')}/{rel}?v={int(time.time())}")
            if got is None:
                bad.append((edge, rel, "network"))
            elif hashlib.md5(got).hexdigest() != local[rel]:
                bad.append((edge, rel, "differs"))
    return bad


# ---------------------------------------------------------------- AWS side (boto3), kept thin
def live_etags(s3, bucket: str, prefix: str = "") -> dict:
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            out[o["Key"]] = o["ETag"].strip('"')
    return out


def upload_one(s3, bucket: str, root: Path, prefix: str, rel: str) -> str:
    key = f"{prefix}/{rel}" if prefix else rel
    body = (root / rel).read_bytes()
    for attempt in (1, 2, 3):
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type(rel), CacheControl=cache_control(rel))
            return ""
        except Exception as e:  # a transient AWS stall is cheap to retry
            err = str(e)[:120]; time.sleep(2 * attempt)
    return err


def fetch_url(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=25) as h:
            return h.read()
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", required=True, help="local build folder")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="", help="key prefix inside the bucket (folder-per-site layouts)")
    ap.add_argument("--distribution", action="append", default=[], help="CloudFront id to invalidate; repeat for every distribution in front of the bucket")
    ap.add_argument("--edge", action="append", default=[], help="base URL of an edge host serving this build; repeat per host")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--force", action="store_true", help="upload every file even when the ETag matches (repairs metadata)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    import boto3
    s3, cf = boto3.client("s3"), boto3.client("cloudfront")
    root = Path(a.dir).resolve(); prefix = a.prefix.strip("/")
    t0 = time.time()
    local = local_manifest(root)
    live = live_etags(s3, a.bucket, prefix)
    p = plan(local, live, prefix, a.force)
    todo = p["new"] + p["changed"]
    print(f"{len(local)} local files, {len(live)} live objects under '{prefix or '/'}': "
          f"{len(p['new'])} new, {len(p['changed'])} changed, {len(p['same'])} unchanged ({time.time() - t0:.0f}s)")
    if a.dry_run or not todo:
        print("dry run, nothing uploaded" if a.dry_run else "nothing to upload"); return 0
    failed = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for rel, err in zip(todo, ex.map(lambda r: upload_one(s3, a.bucket, root, prefix, r), todo)):
            if err: failed.append((rel, err))
    print(f"uploaded {len(todo) - len(failed)} files, {len(failed)} failed, {(time.time() - t0) / 60:.1f} min")
    for rel, err in failed[:20]: print("   FAILED", rel, err)
    for dist in a.distribution:
        inv = cf.create_invalidation(DistributionId=dist, InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": invalidation_paths()}, "CallerReference": f"s3deploy-{int(time.time() * 1000)}"})["Invalidation"]["Id"]
        print(f"invalidation {inv} on {dist}: waiting")
        cf.get_waiter("invalidation_completed").wait(DistributionId=dist, Id=inv)
    stale = verify_origin(local, live_etags(s3, a.bucket, prefix), prefix)
    print(f"origin: {len(local) - len(stale)} of {len(local)} files match the build, {len(stale)} do not")
    for rel in stale[:20]: print("   STALE", rel)
    bad = verify_edge(local, fetch_url, a.edge) if a.edge else []
    if a.edge:
        net = [b for b in bad if b[2] == "network"]; dif = [b for b in bad if b[2] == "differs"]
        print(f"edge: {len(a.edge)} host(s), {len(dif)} files differ, {len(net)} could not be fetched")
        for b in bad[:20]: print("   ", *b)
    print(f"done in {(time.time() - t0) / 60:.1f} min")
    return 1 if (failed or stale or any(b[2] == "differs" for b in bad)) else 0


if __name__ == "__main__":
    sys.exit(main())
