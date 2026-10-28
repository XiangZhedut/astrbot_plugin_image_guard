"""Routing metadata only; no image content or identity inference."""

import re


def group_from_session(platform, session, sender=""):
    if ":GroupMessage:" not in session:
        return ""
    prefix, group_id = session.split(":GroupMessage:", 1)
    platform = prefix or platform
    if re.fullmatch(r"[0-9A-Fa-f]{32}_[0-9A-Fa-f]{32}", group_id):
        group_id = group_id.rsplit("_", 1)[1]
    elif sender and group_id.startswith(str(sender) + "_"):
        group_id = group_id[len(str(sender)) + 1 :]
    return f"{platform}:GroupMessage:{group_id}" if platform and group_id else ""


def normalize_destination(value):
    try:
        session = str(value or "")
        platform, kind, identifier = session.split(":", 2)
        if not platform or not identifier:
            return ""
        if kind == "GroupMessage":
            return group_from_session(platform, session)
        if kind == "FriendMessage":
            return session
    except (ValueError, TypeError):
        pass
    return ""


def event_destination(event):
    if event is None:
        return ""
    try:
        group = event.get_group_id()
        if group:
            return normalize_destination(f"{event.get_platform_id()}:GroupMessage:{group}")
    except (AttributeError, TypeError):
        pass
    try:
        return normalize_destination(event.unified_msg_origin)
    except (AttributeError, TypeError):
        return ""
