"""Route mailbox operations without provider conditionals in the UI."""
import mail_service as icloud


def backend(credential):
    if getattr(credential, "provider", None) == "gmail":
        import gmail_service
        return gmail_service
    return icloud


def scan(store, credential, limit, progress):
    return backend(credential).scan(store, credential, limit, progress)


def read_message(store, credential, validity, item, progress):
    return backend(credential).read_message(store, credential, validity, item, progress)


def execute(store, credential, preview, selected_uids, progress):
    return backend(credential).execute(store, credential, preview, selected_uids, progress)


def undo(store, credential, progress):
    return backend(credential).undo(store, credential, progress)


def provider_name(credential):
    return "Gmail" if getattr(credential, "provider", None) == "gmail" else "iCloud"
