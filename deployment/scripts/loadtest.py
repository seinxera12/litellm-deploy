"""
Local plumbing load test — proves the gateway handles concurrency without errors.
NOT a performance benchmark. Use 3-5 workers locally; 20-30 workers on prod (Day 5).
Usage: python loadtest.py [--workers N] [--requests N]
"""
import argparse, time, concurrent.futures, requests, statistics

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
VKEY = ""   # set via --key or env

PAYLOAD = {
    "model": "qfind-chat",
    "messages": [{"role": "user", "content": "In one sentence, what is Qfind?"}],
    "stream": False,
    "max_tokens": 50,
}

def single_request(key: str) -> tuple[int, float]:
    start = time.monotonic()
    try:
        r = requests.post(LITELLM_URL, json=PAYLOAD,
                          headers={"Authorization": f"Bearer {key}"},
                          timeout=60)
        return r.status_code, time.monotonic() - start
    except Exception as e:
        print(f"  ERROR: {e}")
        return 0, time.monotonic() - start

def run(workers: int, n_requests: int, key: str):
    print(f"\nLoad test: {n_requests} requests, {workers} concurrent workers\n")
    latencies, errors = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(single_request, key) for _ in range(n_requests)]
        for f in concurrent.futures.as_completed(futures):
            status, lat = f.result()
            if status == 200:
                latencies.append(lat)
            else:
                errors += 1
                print(f"  Non-200 status: {status}")

    print(f"\nResults:")
    print(f"  Success: {len(latencies)}/{n_requests}")
    print(f"  Errors:  {errors}")
    if latencies:
        print(f"  Latency  min={min(latencies):.2f}s  median={statistics.median(latencies):.2f}s  max={max(latencies):.2f}s")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workers",  type=int, default=3)
    p.add_argument("--requests", type=int, default=6)
    p.add_argument("--key",      type=str, required=True)
    args = p.parse_args()
    run(args.workers, args.requests, args.key)