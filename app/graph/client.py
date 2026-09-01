from collections.abc import Iterator
from datetime import datetime

import httpx
import msal

from app.graph.models import GraphFolder, GraphMailbox, GraphMessageRef, parse_graph_datetime
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

    _FOLDER_SELECT = "id,displayName,parentFolderId,wellKnownName,childFolderCount"

    def list_mail_folders(self, user_id: str) -> list[GraphFolder]:
        top_level = list(
            self._paged_get(f"/users/{user_id}/mailFolders", params={"$select": self._FOLDER_SELECT, "$top": "999"})
        )
        folders: list[GraphFolder] = []
        for raw in top_level:
            folders.append(self._to_folder(raw))
            if raw.get("childFolderCount", 0) > 0:
                folders.extend(self._list_child_folders(user_id, raw["id"]))
        return folders

    def _list_child_folders(self, user_id: str, parent_id: str) -> list[GraphFolder]:
        result: list[GraphFolder] = []
        children = list(
            self._paged_get(
                f"/users/{user_id}/mailFolders/{parent_id}/childFolders",
                params={"$select": self._FOLDER_SELECT, "$top": "999"},
            )
        )
        for raw in children:
            result.append(self._to_folder(raw))
            if raw.get("childFolderCount", 0) > 0:
                result.extend(self._list_child_folders(user_id, raw["id"]))
        return result

    @staticmethod
    def _to_folder(raw: dict) -> GraphFolder:
        return GraphFolder(
            id=raw["id"],
            display_name=raw["displayName"],
            parent_id=raw.get("parentFolderId"),
            well_known_name=raw.get("wellKnownName"),
            child_folder_count=raw.get("childFolderCount", 0),
        )

    def list_messages(
        self, user_id: str, folder_id: str, since: datetime | None = None
    ) -> Iterator[GraphMessageRef]:
        params: dict[str, str] = {"$select": "id,receivedDateTime,isRead,flag", "$top": "50"}
        if since is not None:
            params["$filter"] = f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        for raw in self._paged_get(f"/users/{user_id}/mailFolders/{folder_id}/messages", params=params):
            yield GraphMessageRef(
                id=raw["id"],
                received_date_time=parse_graph_datetime(raw["receivedDateTime"]),
                is_read=raw.get("isRead", False),
                is_flagged=(raw.get("flag") or {}).get("flagStatus") == "flagged",
            )

    def get_message_raw(self, user_id: str, message_id: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._token()}"}
        response = graph_request(
            self._http, "GET", f"/users/{user_id}/messages/{message_id}/$value", headers=headers
        )
        return response.content
