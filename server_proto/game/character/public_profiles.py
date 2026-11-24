"""Resolve an inspected actor from the current delivered Game selection."""
from game.raid.runtime import RaidError

def resolve_profile_target(server, repo, session, target_key):
    cid = int(getattr(session, 'selected_character_id', 0) or 0)
    if target_key in (0, 65535, cid):
        account = repo.get_account_id_for_character(cid) if cid else None
        return int(account if account is not None else server.account_id), None, None
    directory = server.native_game_connections
    owner = directory.chat_selection(session, sender=True)
    matches = [s for s in directory.chat_selections() if s[3] == owner[3] and s[4] == target_key]
    if len(matches) != 1:
        raise RaidError('profile target is not a unique online same-channel actor')
    selected = matches[0]
    if repo.get_account_id_for_character(target_key) != selected[2]:
        raise RaidError('profile target no longer belongs to its authenticated connection')
    require_profile_target_current(server, session, owner, selected)
    return selected[2], owner, selected

def require_profile_target_current(server, session, owner, selected):
    if owner is None:
        return
    directory = server.native_game_connections
    if (directory.chat_selection(session, sender=True) != owner
            or directory.chat_selection(selected[0].session) != selected):
        raise RaidError('profile target selection changed while reading')
