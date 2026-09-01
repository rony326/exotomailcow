from app.graph.models import GraphFolder
from app.importers.folder_mapping import build_folder_paths, build_imap_path


def test_build_folder_paths_resolves_nested_hierarchy():
    folders = [
        GraphFolder(id="root", display_name="Projekte", parent_id=None, well_known_name=None, child_folder_count=1),
        GraphFolder(id="child", display_name="2024", parent_id="root", well_known_name=None, child_folder_count=0),
    ]
    paths = build_folder_paths(folders)
    assert paths["root"] == "Projekte"
    assert paths["child"] == "Projekte/2024"


def test_build_imap_path_maps_well_known_folders():
    assert build_imap_path("Inbox", "inbox", ".") == "INBOX"
    assert build_imap_path("Sent Items", "sentitems", ".") == "Sent"
    assert build_imap_path("Deleted Items", "deleteditems", ".") == "Trash"
    assert build_imap_path("Drafts", "drafts", ".") == "Drafts"
    assert build_imap_path("Archive", "archive", ".") == "Archive"


def test_build_imap_path_joins_custom_path_with_delimiter():
    assert build_imap_path("Projekte/2024", None, ".") == "Projekte.2024"
    assert build_imap_path("Projekte/2024", None, "/") == "Projekte/2024"


def test_build_imap_path_sanitizes_delimiter_collision_in_segment():
    assert build_imap_path("A.B/C", None, ".") == "A_B.C"
