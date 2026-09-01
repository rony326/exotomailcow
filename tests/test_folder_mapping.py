from app.graph.models import GraphFolder
from app.importers.folder_mapping import build_folder_paths, build_imap_paths


def test_build_folder_paths_resolves_nested_hierarchy():
    folders = [
        GraphFolder(id="root", display_name="Projekte", parent_id=None, well_known_name=None, child_folder_count=1),
        GraphFolder(id="child", display_name="2024", parent_id="root", well_known_name=None, child_folder_count=0),
    ]
    paths = build_folder_paths(folders)
    assert paths["root"] == "Projekte"
    assert paths["child"] == "Projekte/2024"


def test_build_imap_paths_maps_well_known_folders():
    folders = [
        GraphFolder(id="inbox", display_name="Inbox", parent_id=None, well_known_name="inbox", child_folder_count=0),
        GraphFolder(id="sent", display_name="Sent Items", parent_id=None, well_known_name="sentitems", child_folder_count=0),
        GraphFolder(id="trash", display_name="Deleted Items", parent_id=None, well_known_name="deleteditems", child_folder_count=0),
        GraphFolder(id="drafts", display_name="Drafts", parent_id=None, well_known_name="drafts", child_folder_count=0),
        GraphFolder(id="archive", display_name="Archive", parent_id=None, well_known_name="archive", child_folder_count=0),
    ]
    paths = build_imap_paths(folders, ".")
    assert paths["inbox"] == "INBOX"
    assert paths["sent"] == "Sent"
    assert paths["trash"] == "Trash"
    assert paths["drafts"] == "Drafts"
    assert paths["archive"] == "Archive"


def test_build_imap_paths_joins_custom_path_with_delimiter():
    folders = [
        GraphFolder(id="root", display_name="Projekte", parent_id=None, well_known_name=None, child_folder_count=1),
        GraphFolder(id="child", display_name="2024", parent_id="root", well_known_name=None, child_folder_count=0),
    ]
    assert build_imap_paths(folders, ".")["child"] == "Projekte.2024"
    assert build_imap_paths(folders, "/")["child"] == "Projekte/2024"


def test_build_imap_paths_sanitizes_delimiter_collision_in_segment():
    folders = [
        GraphFolder(id="a", display_name="A.B", parent_id=None, well_known_name=None, child_folder_count=1),
        GraphFolder(id="c", display_name="C", parent_id="a", well_known_name=None, child_folder_count=0),
    ]
    assert build_imap_paths(folders, ".")["c"] == "A_B.C"


def test_build_imap_paths_merges_children_of_well_known_folder_into_mapped_name():
    # Regression: a child of Inbox previously got the raw display-name
    # ancestor segment ("Posteingang/Kunden" -> "Posteingang.Kunden"),
    # creating a *second*, separate "Posteingang" mailbox in Mailcow
    # instead of nesting under the real INBOX -- the child folder itself
    # still ended up correctly nested, just under the wrong new parent,
    # which is why only the top-level Inbox merge looked broken.
    folders = [
        GraphFolder(id="inbox", display_name="Posteingang", parent_id=None, well_known_name="inbox", child_folder_count=1),
        GraphFolder(id="child", display_name="Kunden", parent_id="inbox", well_known_name=None, child_folder_count=0),
    ]
    paths = build_imap_paths(folders, ".")
    assert paths["inbox"] == "INBOX"
    assert paths["child"] == "INBOX.Kunden"
