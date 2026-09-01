import httpx

from app.importers.base import MailcowTarget


class CardDavContactImporter:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http = http_client or httpx.Client()

    def put_contact(self, target: MailcowTarget, uid: str, vcard_data: bytes) -> str:
        url = f"{target.dav_base_url}/SOGo/dav/{target.address}/Contacts/{uid}.vcf"
        response = self._http.put(
            url,
            content=vcard_data,
            auth=(target.address, target.app_password),
            headers={"Content-Type": "text/vcard; charset=utf-8"},
        )
        response.raise_for_status()
        return url
