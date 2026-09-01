from app.graph.models import GraphFolder

_WELL_KNOWN_MAP = {
    "inbox": "INBOX",
    "sentitems": "Sent",
    "deleteditems": "Trash",
    "drafts": "Drafts",
    "archive": "Archive",
}


def build_folder_paths(folders: list[GraphFolder]) -> dict[str, str]:
    by_id = {f.id: f for f in folders}
    paths: dict[str, str] = {}

    def resolve(folder_id: str) -> str:
        if folder_id in paths:
            return paths[folder_id]
        folder = by_id[folder_id]
        if folder.parent_id and folder.parent_id in by_id:
            path = f"{resolve(folder.parent_id)}/{folder.display_name}"
        else:
            path = folder.display_name
        paths[folder_id] = path
        return path

    for folder in folders:
        resolve(folder.id)
    return paths


def build_imap_paths(folders: list[GraphFolder], delimiter: str) -> dict[str, str]:
    """Maps every folder to its target IMAP path.

    Substitutes the Mailcow/Dovecot well-known mailbox name (INBOX, Sent,
    Trash, ...) for a well-known folder's own segment *and* for that
    segment wherever it appears as an ancestor of a child folder. Using the
    raw Graph display name ("Inbox") for the ancestor segment instead of
    "INBOX" previously created a second, separate "Inbox" mailbox in
    Mailcow rather than merging into the real INBOX -- children still
    nested correctly under that wrong new folder, which is why it only
    showed up as "children come through fine, but Inbox itself doesn't
    merge."
    """
    by_id = {f.id: f for f in folders}
    paths: dict[str, str] = {}

    def resolve(folder_id: str) -> str:
        if folder_id in paths:
            return paths[folder_id]
        folder = by_id[folder_id]
        segment = _WELL_KNOWN_MAP.get(folder.well_known_name, folder.display_name.replace(delimiter, "_"))
        if folder.parent_id and folder.parent_id in by_id:
            path = f"{resolve(folder.parent_id)}{delimiter}{segment}"
        else:
            path = segment
        paths[folder_id] = path
        return path

    for folder in folders:
        resolve(folder.id)
    return paths
