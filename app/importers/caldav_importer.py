import httpx

from app.importers.base import MailcowTarget


class CalDavCalendarImporter:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http = http_client or httpx.Client()

    def put_event(self, target: MailcowTarget, uid: str, ics_data: bytes) -> str:
        url = f"{target.dav_base_url}/SOGo/dav/{target.address}/Calendar/{uid}.ics"
        response = self._http.put(
            url,
            content=ics_data,
            auth=(target.address, target.app_password),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        response.raise_for_status()
        return url
