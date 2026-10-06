"""Gmail Inbox provider. No send/compose or permanent-delete endpoints."""
import base64
from concurrent.futures import ThreadPoolExecutor
from email.utils import parseaddr
from urllib.parse import quote, urlparse
import json

import mail_service as shared
from mail_service import MailError
from mail_helpers import decode_header_text, parse_unsubscribe, classify_subject

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
HEADERS = ["From", "Subject", "Date", "Message-ID", "List-ID", "List-Unsubscribe", "List-Unsubscribe-Post"]


def request(api, method, path, **kwargs):
    try:
        response = api.request(method, BASE + path, timeout=(10, 30), **kwargs)
    except Exception as exc:
        raise MailError("gmail_access" if type(exc).__name__ == "RefreshError" else "gmail_network") from None
    if response.status_code == 404:
        return None
    if response.status_code in (401, 403):
        raise MailError("gmail_access")
    if response.status_code == 429:
        raise MailError("gmail_rate_limit")
    if not 200 <= response.status_code < 300:
        raise MailError("gmail_api")
    return response.json()


def validate_account(store, credential, api):
    if store.account != "gmail:" + credential.account:
        raise MailError("stale")
    profile = request(api, "GET", "/profile")
    if not profile or profile.get("emailAddress", "").lower() != credential.account:
        raise MailError("stale")
    return "gmail:" + credential.account


def message_path(uid):
    return "/messages/" + quote(str(uid), safe="")


def metadata(data):
    headers = {h["name"].casefold(): h.get("value", "") for h in data.get("payload", {}).get("headers", [])}
    name, sender = parseaddr(headers.get("from", ""))
    subject = decode_header_text(headers.get("subject", ""))
    return dict(
        uid=data["id"], sender=sender.lower().strip(), name=decode_header_text(name),
        subject=subject, date=decode_header_text(headers.get("date", "")),
        received=int(data.get("internalDate", 0)) / 1000,
        unread="UNREAD" in data.get("labelIds", []), kind=classify_subject(subject),
        message_id=headers.get("message-id", ""), list_id=headers.get("list-id", ""),
        urls=parse_unsubscribe(headers.get("list-unsubscribe", "")),
        one_click="list-unsubscribe=one-click" in headers.get("list-unsubscribe-post", "").lower(),
        labels=data.get("labelIds", []), snippet=data.get("snippet", ""),
    )


def get_metadata(api, uid):
    return request(api, "GET", message_path(uid), params={"format": "metadata", "metadataHeaders": HEADERS})


def scan(store, credential, limit, progress):
    with shared.account_lock(store.account), credential.lock:
        with credential.api() as api:
            validity = validate_account(store, credential, api)
            progress("connect", 0, 0)
            ids, token = [], None
            while True:
                params = {"labelIds": "INBOX", "maxResults": 500}
                if token:
                    params["pageToken"] = token
                page = request(api, "GET", "/messages", params=params)
                ids.extend(m["id"] for m in page.get("messages", []))
                progress("scan", len(ids), 0)
                token = page.get("nextPageToken")
                if not token:
                    break
            ids = list(dict.fromkeys(ids))
            selected = ids[:limit] if limit else ids

        def load(uid):
            with credential.api() as api:
                return get_metadata(api, uid)

        messages = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, data in enumerate(pool.map(load, selected)):
                if data and "INBOX" in data.get("labelIds", []):
                    messages.append(metadata(data))
                progress("scan", i + 1, len(selected))
        progress("analyse", 0, 0)
        total = len(messages) if not limit else len(ids)
        store.save_scan(validity, total, messages)
        return dict(scanned=len(messages), total=total)


def verify_identity(data, old, required_label=None):
    if not data:
        raise MailError("message_missing")
    live = metadata(data)
    if (live["sender"], live["message_id"], live["subject"]) != (old["sender"], old["message_id"], old["subject"]):
        raise MailError("stale")
    if required_label and required_label not in data.get("labelIds", []):
        raise MailError("stale")


def read_message(store, credential, validity, item, progress):
    with shared.account_lock(store.account), credential.lock, credential.api() as api:
        if validate_account(store, credential, api) != validity:
            raise MailError("stale")
        progress("read", 0, 1)
        verify_identity(get_metadata(api, item["uid"]), item, "INBOX")
        data = request(api, "GET", message_path(item["uid"]), params={"format": "raw"})
        if not data or not data.get("raw"):
            raise MailError("message_missing")
        raw = base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))
        limit = 262144
        text = shared.message_text(raw[:limit])
        progress("read", 1, 1)
        return dict(uid=item["uid"], text=text[:limit], truncated=len(raw) > limit)


def execute(store, credential, preview, selected_uids, progress):
    with shared.account_lock(store.account), credential.lock:
        white = {s for s, r in store.rules().items() if r["policy"] == "white"}
        if set(preview["senders"]) & white and not preview["allow_white"]:
            raise MailError("protected")
        targets = [m for m in preview["targets"] if m["uid"] in set(selected_uids)]
        if preview["mode"] == "delete_only" and not targets:
            raise MailError("no_messages")
        result = dict(moved=0, requested=0, manual=0, failed=0, skipped=0,
                      links=[], companies=preview["companies"], outcomes=[])
        op = store.operation(preview["mode"], {"companies": preview["companies"], "provider": "gmail"})
        try:
            with credential.api() as api:
                if validate_account(store, credential, api) != preview["validity"]:
                    raise MailError("stale")
                # Validate every frozen identity before any unsubscribe side effect.
                checked = set()
                for item in targets + preview["unsubs"]:
                    if item["uid"] not in checked:
                        verify_identity(get_metadata(api, item["uid"]), item, "INBOX")
                        checked.add(item["uid"])
                store.trace(op, "identities_verified", count=len(checked))
                if preview["mode"] != "delete_only":
                    for i, item in enumerate(preview["unsubs"]):
                        progress("unsubscribe", i, len(preview["unsubs"]))
                        status, detail = shared.one_click(item)
                        result[status if status in ("requested", "manual", "failed") else "failed"] += 1
                        result["outcomes"].append(dict(sender=item["sender"], action="unsubscribe", status=status))
                        if status != "requested":
                            result["links"].extend((item["sender"], u) for u in item["urls"] if urlparse(u).scheme in ("http", "https", "mailto"))
                        store.unsubscribe(item["sender"], status, detail)
                        store.trace(op, "unsubscribe_result", sender=item["sender"], status=status)
                        progress("unsubscribe", i + 1, len(preview["unsubs"]))
                for i, item in enumerate(targets):
                    progress("delete", i, len(targets))
                    verify_identity(get_metadata(api, item["uid"]), item, "INBOX")
                    ident = store.prepare_move(op, "INBOX", preview["validity"], item, "TRASH")
                    # Persist uncertainty before sending; an interrupted request is not replayed.
                    store.move_state(ident, "uncertain", preview["validity"], item["uid"])
                    request(api, "POST", message_path(item["uid"]) + "/trash", json={})
                    live = get_metadata(api, item["uid"])
                    verify_identity(live, item, "TRASH")
                    if "INBOX" in live.get("labelIds", []):
                        raise MailError("move_unconfirmed")
                    store.move_state(ident, "moved", preview["validity"], item["uid"])
                    store.remove_cached(preview["validity"], item["uid"])
                    result["moved"] += 1
                    store.trace(op, "message_done", uid=item["uid"])
                    progress("delete", i + 1, len(targets))
            store.finish(op, "done", result)
        except Exception as exc:
            result["error"] = shared.error_code(exc)
            result["error_detail"] = shared.error_details(exc)
            store.finish(op, "partial", result)
        return result


def undo(store, credential, progress):
    with shared.account_lock(store.account), credential.lock:
        eligible = [m for m in store.last_moves() if m["state"] == "moved" and m["dest_uid"]]
        if not eligible:
            raise MailError("undo_unavailable")
        op = store.operation("undo", {"count": len(eligible), "provider": "gmail"})
        result = dict(restored=0, skipped=0, failed=0)
        try:
            with credential.api() as api:
                validity = validate_account(store, credential, api)
                for i, row in enumerate(eligible):
                    if row["dest_validity"] != validity:
                        raise MailError("stale")
                    progress("undo", i, len(eligible))
                    old = json.loads(row["message"])
                    verify_identity(get_metadata(api, row["dest_uid"]), old, "TRASH")
                    store.move_state(row["id"], "restoring")
                    request(api, "POST", message_path(row["dest_uid"]) + "/untrash", json={})
                    request(api, "POST", message_path(row["dest_uid"]) + "/modify", json={"addLabelIds": ["INBOX"], "removeLabelIds": ["TRASH"]})
                    live = get_metadata(api, row["dest_uid"])
                    verify_identity(live, old, "INBOX")
                    if "TRASH" in live.get("labelIds", []):
                        raise MailError("move_unconfirmed")
                    store.move_state(row["id"], "restored")
                    store.invalidate_scan()
                    result["restored"] += 1
                    progress("undo", i + 1, len(eligible))
            store.finish(op, "done", result)
        except Exception as exc:
            result["error"] = shared.error_code(exc)
            result["error_detail"] = shared.error_details(exc)
            store.finish(op, "partial", result)
        return result
