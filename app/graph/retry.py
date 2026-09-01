import random
import time

import httpx

MAX_RETRIES = 5


def graph_request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    attempt = 0
    while True:
        response = client.request(method, url, **kwargs)
        if response.status_code == 429 or response.status_code >= 500:
            attempt += 1
            if attempt > MAX_RETRIES:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after is not None else min(2**attempt, 60) + random.uniform(0, 1)
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response
