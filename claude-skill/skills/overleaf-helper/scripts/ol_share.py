"""share: the share links of a project, as the Share dialog of the editor shows them. While link
sharing is on, anyone with the edit link can edit the project and anyone with the view link can read
it; only the owner can turn it on or off.
"""
import ol_api
import ol_sync

TOKEN_BASED = "tokenBased"  # publicAccesLevel of a project with link sharing on


def links(api, project_id, turn=None):
    """{sharing, edit, view} of the project: whether link sharing is on, and its edit and view links,
    None where Overleaf gives none. turn True or False first turns link sharing on or off; for that
    the account must own the project."""
    with ol_api.Realtime(api, project_id) as live:
        level = live.project.get("publicAccesLevel")  # sic: Overleaf's name of the field
        owner = live.permissions == "owner"
    if turn is not None and turn != (level == TOKEN_BASED):
        if not owner:
            raise ol_sync.SyncError("Only the owner of the project can turn link sharing on or off.")
        api.set_link_sharing(project_id, turn)
        level = TOKEN_BASED if turn else "private"
    tokens = api.share_tokens(project_id) if level == TOKEN_BASED else {}
    return {"sharing": level == TOKEN_BASED,
            "edit": f"{ol_api.BASE_URL}/{tokens['readAndWrite']}" if tokens.get("readAndWrite") else None,
            "view": f"{ol_api.BASE_URL}/read/{tokens['readOnly']}" if tokens.get("readOnly") else None}
