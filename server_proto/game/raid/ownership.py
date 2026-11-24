"""A raid channel alone does not give an actor raid membership."""


def current_raid_entry(server, character_id):
    if type(character_id) is not int or character_id <= 0:
        return None
    service = getattr(server, 'raid_service', None)
    return service.current(character_id) if service is not None else None


def owns_raid_membership(server, session):
    return current_raid_entry(server, getattr(session, 'selected_character_id', None)) is not None
