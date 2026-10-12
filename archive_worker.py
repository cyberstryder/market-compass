#!/usr/bin/env python3
"""Standalone R2 archive worker - bypasses HTTP 60s timeout.

Runs the COPY archive directly without going through the dashboard HTTP endpoint.
This avoids Railway's HTTP request timeout that kills long-running COPY operations.

Usage:
    python3 archive_worker.py [--chunks N] [--kind KIND]

The worker processes archive chunks until complete or max chunks reached.
Progress is tracked in DB, so it's safe to run repeatedly.
"""
import os
import sys
import time

# Add the repo to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    import argparse
    parser = argparse.ArgumentParser(description='R2 archive worker')
    parser.add_argument('--chunks', type=int, default=6,
                       help='Max chunks to process (default: 6)')
    parser.add_argument('--kind', type=str, default=None,
                       help='Only process specific kind (e.g., quote)')
    args = parser.parse_args()

    # Import after path setup
    from compass.config import Config
    from compass.store import Store
    from compass.copy_archive import run_copy_chunk, _s3_client

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Archive worker starting", flush=True)
    print(f"  Max chunks: {args.chunks}", flush=True)
    if args.kind:
        print(f"  Kind filter: {args.kind}", flush=True)

    # Get DB and S3 client
    cfg = Config()
    db = Store(cfg.db)
    client = _s3_client()
    bucket = os.environ['ARCHIVE_BUCKET']

    print(f"  Bucket: {bucket}", flush=True)

    # Run the archive
    start = time.time()
    try:
        result = run_copy_chunk(db, client, bucket, max_months=args.chunks)
        elapsed = time.time() - start

        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Complete:", flush=True)
        print(f"  Chunks done: {result.get('chunks_done', 0)}", flush=True)
        print(f"  Rows: {result.get('rows', 0):,}", flush=True)
        print(f"  Bytes: {result.get('bytes', 0):,}", flush=True)
        print(f"  Elapsed: {elapsed:.1f}s", flush=True)
        if result.get('chunks'):
            for c in result['chunks']:
                print(f"    - {c}", flush=True)
        if result.get('error'):
            print(f"  ERROR: {result['error']}", flush=True)
            sys.exit(1)
        if not result.get('complete'):
            print(f"  More chunks remain (not complete)", flush=True)

    except Exception as e:
        import traceback
        print(f"\nFATAL ERROR: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
