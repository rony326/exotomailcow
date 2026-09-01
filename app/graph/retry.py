import random
import time

import httpx

MAX_RETRIES = 5


def _graph_error_detail(response: httpx.Response) -> str | None:
    # Graph puts the actually useful diagnostic in the JSON body
    # ({"error": {"code": "...", "message": "..."}}), e.g. "Mailbox
    # NotEnabledForRESTAPI: The mailbox is either inactive, soft-deleted, or
    # is hosted on-premise." httpx's default raise_for_status() message is
    # just "Client error '400 Bad Request' for url '...'" -- useless for an
    # operator trying to figure out what's actually wrong.
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    message = error.get("message")
    if code and message:
        return f"{code}: {message}"
    return code or message


def _raise_with_graph_detail(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _graph_error_detail(response)
        if detail:
            raise httpx.HTTPStatusError(f"{exc} -- {detail}", request=exc.request, response=exc.response) from exc
        raise


def graph_request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    attempt = 0
    while True:
        response = client.request(method, url, **kwargs)
        if response.status_code == 429 or response.status_code >= 500:
            attempt += 1
            if attempt > MAX_RETRIES:
                _raise_with_graph_detail(response)
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after is not None else min(2**attempt, 60) + random.uniform(0, 1)
            time.sleep(delay)
            continue
        _raise_with_graph_detail(response)
        return response
