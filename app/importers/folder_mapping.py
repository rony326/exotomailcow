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


def build_imap_path(graph_path: str, well_known_type: str | None, delimiter: str) -> str:
    if well_known_type and well_known_type in _WELL_KNOWN_MAP:
        return _WELL_KNOWN_MAP[well_known_type]
    segments = [segment.replace(delimiter, "_") for segment in graph_path.split("/") if segment]
    return delimiter.join(segments)
