"""IMAP operations. Workers never call Streamlit; progress uses a callback."""

import email
from email import policy
from html.parser import HTMLParser
import imaplib
import json
import re
import ssl
import threading
from collections import defaultdict
from contextlib import contextmanager
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import urlparse
import requests
from mail_helpers import (
    decode_header_text,
    parse_unsubscribe,
    classify_subject,
    strict_brand_key,
    pretty_brand_name,
    is_safe_public_http_url,
)


def uid_key(uid):
    """Comparable opaque identifiers: decimal IMAP UIDs or hexadecimal Gmail IDs."""
    text = str(uid)
    try:
        return int(text, 10) if text.isdecimal() else int(text, 16)
    except ValueError:
        return int.from_bytes(text.encode("utf-8"), "big")


class MailError(Exception):
    pass


_LOCKS = defaultdict(threading.Lock)


@contextmanager
def account_lock(account):
    lock = _LOCKS[account.strip().lower()]
    if not lock.acquire(blocking=False):
        raise MailError("busy")
    try:
        yield
    finally:
        lock.release()


def quote(value):
    if any(c in str(value) for c in "\r\n\x00"):
        raise MailError("invalid_input")
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def checked(result):
    if result[0] != "OK":
        raise MailError("server_command")
    return result[1]


@contextmanager
def connection(addr, password):
    m = imaplib.IMAP4_SSL(
        "imap.mail.me.com", 993, ssl_context=ssl.create_default_context(), timeout=30
    )
    try:
        m.login(addr.strip(), password.strip())
        # Refresh capabilities after authentication (some servers advertise MOVE only then).
        caps = checked(m.capability())
        m.capabilities = b" ".join(caps).upper().split()
        yield m
    finally:
        try:
            m.logout()
        except Exception:
            pass


def select(m, folder="INBOX", readonly=True):
    checked(m.select(quote(folder), readonly=readonly))
    _, values = m.response("UIDVALIDITY")
    if not values or not values[0]:
        raise MailError("missing_validity")
    return values[0].decode() if isinstance(values[0], bytes) else str(values[0])


def search(m, *criteria):
    d = checked(m.uid("SEARCH", None, *criteria))
    return [u.decode() for u in (d[0] or b"").split()]


def fetch(m, uids, progress=lambda *a: None):
    messages = []
    for start in range(0, len(uids), 100):
        batch = uids[start : start + 100]
        rows = checked(
            m.uid(
                "FETCH",
                ",".join(batch),
                "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID LIST-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])",
            )
        )
        for row in rows:
            if not isinstance(row, tuple):
                continue
            meta, raw = row[:2]
            uid = re.search(rb"\bUID (\d+)", meta)
            if not uid:
                raise MailError("missing_uid")
            msg = email.message_from_bytes(raw)
            name, sender = parseaddr(msg.get("From", ""))
            subject = decode_header_text(msg.get("Subject", ""))
            flags_match = re.search(rb"\bFLAGS \(([^)]*)\)", meta)
            flags = (
                flags_match[1].lower().split()
                if flags_match
                else []
            )
            unread = b"\\seen" not in flags
            dt = re.search(rb'INTERNALDATE "([^"]+)"', meta)
            try:
                received = (
                    parsedate_to_datetime(dt[1].decode()).timestamp() if dt else 0
                )
            except (ValueError, TypeError, OverflowError):
                received = 0
            urls = parse_unsubscribe(msg.get("List-Unsubscribe", ""))
            messages.append(
                dict(
                    uid=uid[1].decode(),
                    sender=sender.lower().strip(),
                    name=decode_header_text(name),
                    subject=subject,
                    date=decode_header_text(msg.get("Date", "")),
                    received=received,
                    unread=unread,
                    kind=classify_subject(subject),
                    message_id=msg.get("Message-ID", ""),
                    list_id=msg.get("List-ID", ""),
                    urls=urls,
                    one_click="list-unsubscribe=one-click"
                    in msg.get("List-Unsubscribe-Post", "").lower(),
                )
            )
        progress("scan", min(start + len(batch), len(uids)), len(uids))
    return messages


def scan(store, password, limit, progress):
    with account_lock(store.account), connection(store.account, password) as m:
        progress("connect", 0, 0)
        validity = select(m)
        uids = search(m, "ALL")
        total = len(uids)
        selected = uids[-limit:] if limit else uids
        messages = fetch(m, selected, progress)
        progress("analyse", 0, 0)
        store.save_scan(validity, total, messages)
        return dict(scanned=len(messages), total=total)


def companies(store):
    scan = store.scan()
    if not scan:
        return []
    rules = store.rules()
    subs = store.subscriptions()
    after = store.after_unsubscribe()
    deleted = store.recent_deleted()
    groups = {}
    for msg in store.directory():
        sender = msg["sender"]
        rule = rules.get(sender, {})
        key = rule.get("group_id") or strict_brand_key(msg["name"], sender)
        g = groups.setdefault(
            key,
            dict(
                key=key,
                name=rule.get("group_name") or "",
                messages=[],
                senders={},
                latest=0,
            ),
        )
        g["senders"][sender] = msg["name"]
        g["latest"] = max(g["latest"], msg["latest"])
    for msg in scan["messages"]:
        sender = msg["sender"]
        rule = rules.get(sender, {})
        key = rule.get("group_id") or strict_brand_key(msg["name"], sender)
        g = groups.setdefault(
            key,
            dict(
                key=key,
                name=rule.get("group_name") or "",
                messages=[],
                senders={},
                latest=0,
            ),
        )
        g["messages"].append(msg)
        g["senders"][sender] = msg["name"]
        g["latest"] = max(g["latest"], msg["received"])
    for g in groups.values():
        if not g["name"]:
            g["name"] = pretty_brand_name(
                g["key"], [{"Отправитель": n} for n in g["senders"].values()]
            )
        g["protected"] = any(
            rules.get(s, {}).get("policy") == "white" for s in g["senders"]
        )
        g["black"] = any(
            rules.get(s, {}).get("policy") == "black" for s in g["senders"]
        )
        g["after"] = sum(after.get(s, 0) for s in g["senders"])
        g["recent"] = sum(deleted.get(s, 0) for s in g["senders"])
        statuses = {subs.get(s, {}).get("status", "active") for s in g["senders"]}
        g["status"] = (
            "white"
            if g["protected"]
            else (
                "black"
                if g["black"]
                else next(iter(statuses)) if len(statuses) == 1 else "mixed"
            )
        )
    return list(groups.values())


def unsubscribe_targets(messages):
    """Keep separate List-IDs; use one-click URL and its flag from the same message."""
    targets = {}
    for msg in sorted(messages, key=lambda x: (x["received"], uid_key(x["uid"]))):
        if msg["urls"]:
            key = (msg["sender"], msg["list_id"] or "")
            targets[key] = msg
    return list(targets.values())


def matches_read_filter(message, read_filter):
    if read_filter == "unread":
        return message.get("unread") is True
    if read_filter == "read":
        return message.get("unread") is False
    return True


def prepare(store, password, keys, mode, scope, allow_white, read_filter, progress):
    chosen = [g for g in companies(store) if g["key"] in keys]
    if not chosen:
        raise MailError("empty")
    senders = {s for g in chosen for s in g["senders"]}
    white = {s for s, r in store.rules().items() if r["policy"] == "white"}
    if senders & white and not allow_white:
        raise MailError("protected")
    with account_lock(store.account), connection(store.account, password) as m:
        progress("connect", 0, 0)
        validity = select(m)
        uids = set()
        for i, sender in enumerate(sorted(senders)):
            if sender:
                uids.update(search(m, "HEADER", "FROM", quote(sender)))
            progress("prepare", i + 1, len(senders))
        messages = [
            x
            for x in fetch(m, sorted(uids, key=uid_key), progress)
            if x["sender"] in senders and matches_read_filter(x, read_filter)
        ]
    targets = [
        x
        for x in messages
        if mode != "unsubscribe_only" and (scope == "all" or x["kind"] == "promo")
    ]
    return dict(
        keys=sorted(keys),
        mode=mode,
        scope=scope,
        read_filter=read_filter,
        allow_white=allow_white,
        validity=validity,
        targets=targets,
        unsubs=unsubscribe_targets(messages) if mode != "delete_only" else [],
        senders=sorted(senders),
        companies=[g["name"] for g in chosen],
        found=len(messages),
        excluded=len(messages) - len(targets) if mode != "unsubscribe_only" else 0,
    )


class _ReadableHTML(HTMLParser):
    """Extract text only. Never render email HTML or fetch remote resources."""

    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "tr") and not self.hidden:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def message_text(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)
    plain, html = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        data = part.get_payload(decode=True) or b""
        try:
            content = data.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
        except LookupError:
            content = data.decode("utf-8", errors="replace")
        (plain if part.get_content_type() == "text/plain" else html).append(content)
    if plain:
        return "\n\n".join(plain).strip()
    parser = _ReadableHTML()
    parser.feed("\n".join(html))
    return "".join(parser.text).strip()


def read_message(store, password, validity, item, progress):
    with account_lock(store.account), connection(store.account, password) as m:
        progress("read", 0, 1)
        if select(m) != validity:
            raise MailError("stale")
        live = fetch(m, [item["uid"]])
        if not live:
            raise MailError("message_missing")
        if (live[0]["sender"], live[0]["message_id"], live[0]["subject"]) != (
            item["sender"],
            item["message_id"],
            item["subject"],
        ):
            raise MailError("stale")
        limit = 262144
        rows = checked(m.uid("FETCH", item["uid"], f"(UID BODY.PEEK[]<0.{limit}>)"))
        raw = next(
            (
                r[1]
                for r in rows
                if isinstance(r, tuple)
                and re.search(rb"\bUID " + item["uid"].encode() + rb"\b", r[0])
            ),
            None,
        )
        if raw is None:
            raise MailError("message_missing")
        progress("read", 1, 1)
        return dict(
            uid=item["uid"], text=message_text(raw), truncated=len(raw) >= limit
        )


def trash_folder(m):
    rows = checked(m.list())
    fallback = None
    for row in rows:
        if not isinstance(row, bytes):
            continue
        # Parse flags, delimiter, and the full mailbox name (including spaces).
        match = re.match(rb'\(([^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (.+)$', row)
        if not match:
            continue
        name = match[2].decode("ascii")
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        if b"\\trash" in match[1].lower():
            return name
        if name.lower() in {"deleted messages", "trash", "bin", "kosz", "deleted"}:
            fallback = name
    if fallback:
        return fallback
    raise MailError("no_trash")


def copy_mapping(m, uid):
    _, values = m.response("COPYUID")
    for value in values or []:
        if not value:
            continue
        parts = value.decode().split()
        if len(parts) == 3 and parts[1] == str(uid) and parts[2].isdigit():
            return parts[0], parts[2]
    return None, None


def move_one(
    m,
    uid,
    destination,
    record=lambda *a: None,
    trace=lambda *a, **k: None,
):
    """Move one message safely with a simple primary path and diagnostics.

    Prefer UID MOVE when the server advertises it. If the server explicitly
    rejects MOVE and also supports UIDPLUS, fall back to COPY + \\Deleted +
    UID EXPUNGE. Ambiguous network failures are never retried automatically.
    """
    caps = {
        c.decode().upper() if isinstance(c, bytes) else c.upper()
        for c in m.capabilities
    }
    trace("capabilities", move="MOVE" in caps, uidplus="UIDPLUS" in caps)
    m.response("COPYUID")  # discard stale mapping

    if "MOVE" in caps:
        trace("move_send")
        result = m.uid("MOVE", uid, quote(destination))
        status = result[0].decode() if isinstance(result[0], bytes) else result[0]
        if str(status).upper() == "OK":
            trace("move_ok")
            validity, dest_uid = copy_mapping(m, uid)
            trace("move_mapping", mapped=bool(dest_uid))
            trace("verify_source_absent")
            verify_move(m, uid, validity, dest_uid, record)
            trace("verified")
            record("moved", validity, dest_uid)
            return validity, dest_uid

        trace("move_rejected")
        if "UIDPLUS" not in caps:
            checked(result)

    if "UIDPLUS" in caps:
        trace("copy_send")
        checked(m.uid("COPY", uid, quote(destination)))
        trace("copy_ok")
        validity, dest_uid = copy_mapping(m, uid)
        trace("copy_mapping", mapped=bool(dest_uid))
        record("copied", validity, dest_uid)
        if not dest_uid:
            raise MailError("missing_mapping")
        trace("store_deleted_send")
        checked(m.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)"))
        trace("store_deleted_ok")
        trace("uid_expunge_send")
        checked(m.uid("EXPUNGE", uid))
        trace("uid_expunge_ok")
        trace("verify_source_absent")
        verify_move(m, uid, validity, dest_uid, record)
        trace("verified")
        record("moved", validity, dest_uid)
        return validity, dest_uid

    raise MailError("unsafe_move")

def verify_move(m, uid, validity, dest_uid, record):
    """A tagged OK alone must not be displayed as a confirmed deletion."""
    try:
        if str(uid) in search(m, "UID", str(uid)):
            raise MailError("move_unconfirmed")
    except Exception:
        record("uncertain", validity, dest_uid)
        raise


def one_click(msg):
    if not msg["one_click"]:
        return "manual", ""
    candidates = [u for u in msg["urls"] if urlparse(u).scheme == "https"]
    for url in candidates:
        if not is_safe_public_http_url(url):
            continue
        try:
            # Never automatically follow redirects to unchecked hosts.
            with requests.Session() as session:
                session.trust_env = False
                r = session.post(
                    url,
                    data={"List-Unsubscribe": "One-Click"},
                    timeout=(10, 20),
                    allow_redirects=False,
                )
            if 200 <= r.status_code < 300:
                return "requested", f"HTTP {r.status_code}"
        except requests.RequestException:
            pass
    return "failed", "Automatic request was not accepted; use the manual link."


def execute(store, password, preview, selected_uids, progress):
    with account_lock(store.account):
        white = {s for s, r in store.rules().items() if r["policy"] == "white"}
        if set(preview["senders"]) & white and not preview["allow_white"]:
            raise MailError("protected")
        targets = [m for m in preview["targets"] if m["uid"] in selected_uids]
        if preview["mode"] == "delete_only" and not targets:
            raise MailError("no_messages")

        op = store.operation(
            preview["mode"],
            {
                "companies": preview["companies"],
                "target_count": len(targets),
                "unsubscribe_count": len(preview["unsubs"]),
            },
        )

        def trace(step, **fields):
            store.trace(op, step, **fields)

        result = dict(
            moved=0,
            requested=0,
            manual=0,
            failed=0,
            skipped=0,
            links=[],
            companies=preview["companies"],
            outcomes=[],
        )
        trace(
            "operation_started",
            mode=preview["mode"],
            targets=len(targets),
            unsubscribes=len(preview["unsubs"]),
        )
        try:
            # Validate mailbox epoch before any side effect, including unsubscribe.
            trace("connect_start")
            with connection(store.account, password) as m:
                trace("connected")
                progress("connect", 0, 0)
                validity = select(m, readonly=False)
                trace("inbox_selected")
                if validity != preview["validity"]:
                    trace("uidvalidity_changed")
                    raise MailError("stale")
                trace("uidvalidity_ok")

                if preview["mode"] != "delete_only":
                    per_sender = defaultdict(list)
                    for i, item in enumerate(preview["unsubs"]):
                        progress("unsubscribe", i, len(preview["unsubs"]))
                        trace(
                            "unsubscribe_start",
                            index=i + 1,
                            total=len(preview["unsubs"]),
                        )
                        status, detail = one_click(item)
                        trace(
                            "unsubscribe_result",
                            index=i + 1,
                            total=len(preview["unsubs"]),
                            status=status,
                        )
                        per_sender[item["sender"]].append(status)
                        result["outcomes"].append(
                            {
                                "sender": item["sender"],
                                "action": "unsubscribe",
                                "status": status,
                            }
                        )
                        result[status if status in result else "failed"] += 1
                        if status != "requested":
                            result["links"].extend(
                                (item["sender"], url)
                                for url in item["urls"]
                                if urlparse(url).scheme in ("https", "http", "mailto")
                            )
                        # Persist every accepted request even if a later stream fails.
                        store.unsubscribe(item["sender"], status, detail)
                        progress("unsubscribe", i + 1, len(preview["unsubs"]))
                    for sender, statuses in per_sender.items():
                        if len(set(statuses)) > 1:
                            store.unsubscribe(sender, "partial")

                if targets:
                    trace("trash_lookup_start")
                    destination = trash_folder(m)
                    trace("trash_found")
                    caps = {
                        x.decode().upper() if isinstance(x, bytes) else x.upper()
                        for x in m.capabilities
                    }
                    trace(
                        "delete_capabilities",
                        move="MOVE" in caps,
                        uidplus="UIDPLUS" in caps,
                    )
                    if not {"MOVE", "UIDPLUS"} & caps:
                        raise MailError("unsafe_move")

                    for i, item in enumerate(targets):
                        progress("delete", i, len(targets))
                        trace(
                            "message_start",
                            index=i + 1,
                            total=len(targets),
                            uid=item["uid"],
                        )
                        trace(
                            "message_fetch_start",
                            index=i + 1,
                            total=len(targets),
                            uid=item["uid"],
                        )
                        live = fetch(m, [item["uid"]])
                        trace(
                            "message_fetch_done",
                            index=i + 1,
                            total=len(targets),
                            uid=item["uid"],
                            found=bool(live),
                        )
                        if not live:
                            result["skipped"] += 1
                            store.remove_cached(preview["validity"], item["uid"])
                            trace(
                                "message_missing",
                                index=i + 1,
                                total=len(targets),
                                uid=item["uid"],
                            )
                            continue
                        if (
                            live[0]["sender"],
                            live[0]["message_id"],
                            live[0]["subject"],
                        ) != (item["sender"], item["message_id"], item["subject"]):
                            trace(
                                "message_identity_changed",
                                index=i + 1,
                                total=len(targets),
                                uid=item["uid"],
                            )
                            raise MailError("stale")

                        ident = store.prepare_move(
                            op, "INBOX", preview["validity"], item, destination
                        )
                        trace(
                            "move_record_created",
                            index=i + 1,
                            total=len(targets),
                            uid=item["uid"],
                        )
                        try:
                            move_one(
                                m,
                                item["uid"],
                                destination,
                                lambda state, v, u: store.move_state(
                                    ident, state, v, u
                                ),
                                lambda step, **fields: trace(
                                    step,
                                    index=i + 1,
                                    total=len(targets),
                                    uid=item["uid"],
                                    **fields,
                                ),
                            )
                        except Exception:
                            # Do not retry an ambiguous network result automatically.
                            with store.connect() as db:
                                db.execute(
                                    "UPDATE moves SET state='uncertain' WHERE id=? AND state='pending'",
                                    (ident,),
                                )
                            raise

                        result["moved"] += 1
                        result["outcomes"].append(
                            {
                                "sender": item["sender"],
                                "action": "delete",
                                "uid": item["uid"],
                                "status": "moved",
                            }
                        )
                        store.remove_cached(preview["validity"], item["uid"])
                        trace(
                            "message_done",
                            index=i + 1,
                            total=len(targets),
                            uid=item["uid"],
                        )
                        progress("delete", i + 1, len(targets))

            trace(
                "operation_complete",
                moved=result["moved"],
                requested=result["requested"],
                failed=result["failed"],
                skipped=result["skipped"],
            )
            store.finish(op, "done", result)
        except Exception as exc:
            result["error"] = error_code(exc)
            result["error_detail"] = error_details(exc)
            trace(
                "operation_error",
                error=result["error"],
                detail=result["error_detail"],
            )
            store.finish(op, "partial", result)
        return result

def undo(store, password, progress):
    with account_lock(store.account):
        batch = store.last_moves()
        eligible = [
            r
            for r in batch
            if r["state"] == "moved" and r["dest_uid"] and r["dest_validity"]
        ]
        if not eligible:
            raise MailError("undo_unavailable")
        op = store.operation("undo", {"count": len(eligible)})
        result = dict(restored=0, failed=0, skipped=0)
        try:
            with connection(store.account, password) as m:
                for i, row in enumerate(eligible):
                    progress("undo", i, len(eligible))
                    if select(m, row["destination"], False) != row["dest_validity"]:
                        raise MailError("stale")
                    live = fetch(m, [row["dest_uid"]])
                    old = json.loads(row["message"])
                    if not live:
                        store.move_state(row["id"], "missing")
                        result["skipped"] += 1
                        continue
                    if (
                        live[0]["sender"],
                        live[0]["message_id"],
                        live[0]["subject"],
                    ) != (old["sender"], old["message_id"], old["subject"]):
                        raise MailError("stale")
                    store.move_state(row["id"], "restoring")
                    move_one(m, row["dest_uid"], row["source"])
                    store.move_state(row["id"], "restored")
                    result["restored"] += 1
                    store.invalidate_scan()
                    progress("undo", i + 1, len(eligible))
            store.finish(op, "done", result)
        except Exception as exc:
            result["error"] = error_code(exc)
            result["error_detail"] = error_details(exc)
            store.finish(op, "partial", result)
        return result


def error_code(exc):
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, imaplib.IMAP4.error):
        return "imap"
    if isinstance(exc, (TimeoutError, OSError)):
        return "network"
    return "unexpected"


def error_details(exc):
    # Do not persist raw server replies: they can contain mail data or credentials.
    detail = f"{error_code(exc)} · {type(exc).__name__}"
    command = re.search(
        r"\b(UID|MOVE|COPY|STORE|EXPUNGE|FETCH|SEARCH|SELECT|EXAMINE|LOGIN|LIST)\b",
        str(exc),
    )
    if command:
        detail += " · command=" + command[1]
    response = re.search(r"\b(BAD|NO|BYE)\b", str(exc))
    if response:
        detail += " · response=" + response[1]
    return detail
