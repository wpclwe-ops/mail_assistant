"""Inbox browsing and exact-message deletion, independent of the UI."""

from copy import deepcopy
import mail_service as service


def browse(messages, query="", read_filter="all", sort="newest"):
    query = query.strip().casefold()
    matches = [
        m
        for m in messages
        if service.matches_read_filter(m, read_filter)
        and (
            not query
            or query
            in " ".join(
                str(m.get(key, "")) for key in ("subject", "sender", "name")
            ).casefold()
        )
    ]
    if sort in ("sender", "subject"):
        return sorted(
            matches,
            key=lambda m: (
                str(m.get(sort, "")).casefold(),
                -m["received"],
                -service.uid_key(m["uid"]),
            ),
        )
    return sorted(
        matches, key=lambda m: (m["received"], service.uid_key(m["uid"])), reverse=sort != "oldest"
    )


def deletion_preview(store, validity, selected_uids, allow_white=False):
    """Freeze only selected messages; never widen selection to a sender/group."""
    scan = store.scan()
    if not scan or scan["validity"] != validity:
        raise service.MailError("stale")
    selected = set(selected_uids)
    targets = [m for m in scan["messages"] if m["uid"] in selected]
    if not targets:
        raise service.MailError("no_messages")
    if {m["uid"] for m in targets} != selected:
        raise service.MailError("stale")
    senders = sorted({m["sender"] for m in targets})
    white = {s for s, r in store.rules().items() if r["policy"] == "white"}
    if set(senders) & white and not allow_white:
        raise service.MailError("protected")
    return dict(
        keys=[],
        mode="delete_only",
        scope="all",
        read_filter="all",
        allow_white=allow_white,
        validity=validity,
        targets=deepcopy(targets),
        unsubs=[],
        senders=senders,
        companies=senders,
        found=len(targets),
        excluded=0,
    )


def action_preview(store, validity, selected_uids, mode="delete_only", allow_white=False):
    """Freeze exact message and subscription targets for all three actions."""
    if mode not in ("delete_only", "unsubscribe_only", "unsubscribe_delete"):
        raise service.MailError("empty")
    preview = deletion_preview(store, validity, selected_uids, allow_white)
    messages = preview["targets"]
    preview["mode"] = mode
    if mode != "delete_only":
        preview["unsubs"] = service.unsubscribe_targets(messages)
    if mode == "unsubscribe_only":
        preview["targets"] = []
    selected = set(selected_uids)
    preview["companies"] = [
        g["name"] for g in service.companies(store)
        if any(m["uid"] in selected for m in g["messages"])
    ]
    return preview
