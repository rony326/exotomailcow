from collections.abc import Iterator

import httpx
import msal

from app.graph.models import GraphMailbox
from app.graph.retry import graph_request

_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class GraphClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._msal_app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._http = http_client or httpx.Client(base_url=_GRAPH_BASE_URL, timeout=30.0)

    def _token(self) -> str:
        result = self._msal_app.acquire_token_for_client(scopes=_GRAPH_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error_description', result)}")
        return result["access_token"]

    def _get(self, url: str, params: dict | None = None, extra_headers: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if extra_headers:
            headers.update(extra_headers)
        response = graph_request(self._http, "GET", url, headers=headers, params=params)
        return response.json()

    def _paged_get(self, url: str, params: dict | None = None, extra_headers: dict | None = None) -> Iterator[dict]:
        next_url: str | None = url
        next_params: dict | None = params
        while next_url:
            data = self._get(next_url, params=next_params, extra_headers=extra_headers)
            yield from data.get("value", [])
            next_url = data.get("@odata.nextLink")
            next_params = None

    def list_mailboxes(self, search: str | None = None) -> Iterator[GraphMailbox]:
        params: dict[str, str] = {"$select": "id,userPrincipalName,displayName,mail", "$top": "999"}
        if search:
            escaped = search.replace("'", "''")
            params["$filter"] = f"startswith(displayName,'{escaped}') or startswith(userPrincipalName,'{escaped}')"
        for raw in self._paged_get("/users", params=params):
            yield GraphMailbox(
                id=raw["id"],
                user_principal_name=raw["userPrincipalName"],
                display_name=raw.get("displayName") or raw["userPrincipalName"],
                mail=raw.get("mail"),
            )
