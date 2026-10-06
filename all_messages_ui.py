"""Native Streamlit inbox browser; persistent selection is not widget state."""

import math
import re
import pandas as pd
import streamlit as st
import mail_service as service
import mail_provider
from all_messages import browse, action_preview


def reset_confirmation():
    st.session_state.pop("inbox_confirmation", None)
    st.session_state.pop("inbox_allow_white", None)


def reconcile(store, scan):
    ss = st.session_state
    epoch = (store.account, scan["validity"] if scan else None)
    if ss.get("inbox_epoch") != epoch:
        ss.inbox_epoch = epoch
        ss.inbox_selected = []
        ss.inbox_page = 0
        ss.pop("inbox_open_uid", None)
        ss.pop("inbox_company", None)
        ss.company_all = False
        reset_confirmation()
    live = {m["uid"] for m in scan["messages"]} if scan else set()
    ss.inbox_selected = [u for u in ss.get("inbox_selected", []) if u in live]
    if ss.get("inbox_open_uid") not in live:
        ss.pop("inbox_open_uid", None)


def change_selection(uid, key):
    ss = st.session_state
    chosen = set(ss.get("inbox_selected", []))
    if ss[key]:
        chosen.add(uid)
    else:
        chosen.discard(uid)
    ss.inbox_selected = sorted(chosen, key=service.uid_key)
    reset_confirmation()


def select_page(uids):
    ss = st.session_state
    ss.inbox_selected = sorted(set(ss.get("inbox_selected", [])) | set(uids), key=service.uid_key)
    reset_confirmation()


def clear_selection():
    st.session_state.inbox_selected = []
    reset_confirmation()


def reset_page():
    st.session_state.inbox_page = 0


def turn_page(offset):
    st.session_state.inbox_page += offset


def open_message(uid):
    ss = st.session_state
    ss.inbox_open_uid = uid
    # Content is scoped to the currently opened message and mailbox epoch.
    ss.pop("message_content", None)


def close_reader():
    st.session_state.pop("inbox_open_uid", None)
    st.session_state.pop("message_content", None)


def confirm_deletion():
    ss = st.session_state
    preview = ss.pop("inbox_confirmation")
    ss.execute_request = {
        "preview": preview,
        "selected_uids": [m["uid"] for m in preview["targets"]],
    }
    ss.pop("inbox_allow_white", None)


def open_company(key):
    ss = st.session_state
    ss.inbox_return_page = ss.get("inbox_page", 0)
    ss.inbox_return_search = ss.get("inbox_search", "")
    ss.inbox_search = ""
    ss.inbox_company = key
    ss.inbox_page = 0
    close_reader()
    ss.company_all = False
    reset_confirmation()


def close_company():
    ss = st.session_state
    ss.pop("inbox_company", None)
    ss.inbox_page = ss.get("inbox_return_page", 0)
    ss.inbox_search = ss.get("inbox_return_search", "")
    ss.company_all = False
    close_reader()
    reset_confirmation()


def change_group_selection(uids, key):
    ss = st.session_state
    selected = set(ss.get("inbox_selected", []))
    selected = selected | set(uids) if ss[key] else selected - set(uids)
    ss.inbox_selected = sorted(selected, key=service.uid_key)
    reset_confirmation()


def safe_label(value):
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~$])", r"\\\1", value)


VIEW_KEYS = ("inbox_search", "inbox_filter", "mail_display", "inbox_size", "company_all")


def remember_view():
    ss = st.session_state
    state = dict(ss.get("mail_view_state", {}))
    state.update({key: ss[key] for key in VIEW_KEYS if key in ss})
    ss.mail_view_state = state



def render(store, start, T, date, show_error):
    ss = st.session_state
    for key, value in ss.get("mail_view_state", {}).items():
        if key not in ss:
            ss[key] = value
    st.html('<div style="height:28px" aria-hidden="true"></div>')
    scan = store.scan()
    reconcile(store, scan)
    labels = {
        "all": ("Все", "All"),
        "unread": ("Непрочитанные", "Unread"),
        "read": ("Прочитанные", "Read"),
    }
    read_filter = st.radio(
        T("Показывать письма", "Show messages"), list(labels), horizontal=True,
        format_func=lambda v: T(*labels[v]), key="inbox_filter",
        on_change=reset_page,
        disabled=not scan or any("unread" not in m for m in scan["messages"]),
    )
    refresh, undo = st.columns([3, 2])
    if refresh.button(
        T("Обновить почту", "Refresh mail"),
        key="inbox_refresh",
        width="stretch",
    ):
        close_reader()
        reset_confirmation()
        start("scan", mail_provider.scan, store, ss.password, 0)
    if undo.button(
        T("Вернуть последнее удаление", "Undo last deletion"),
        key="inbox_undo",
        width="stretch",
        disabled=not store.last_moves(),
    ):
        close_reader()
        reset_confirmation()
        start("undo", mail_provider.undo, store, ss.password)
    if not scan:
        st.info(
            T(
                "Загрузи письма, чтобы открыть список Входящих.",
                "Load emails to open the inbox list.",
            )
        )
        return
    messages = scan["messages"]
    st.caption(
        T(
            f"Загружено: {len(messages)} из {scan['total']} · обновлено: {date(scan['stamp'])}",
            f"Loaded: {len(messages)} of {scan['total']} · updated: {date(scan['stamp'])}",
        )
    )
    if len(messages) < scan["total"]:
        st.info(
            T(
                "Сейчас показана сохранённая выборка. Нажми «Обновить почту», чтобы получить все Входящие без ограничения.",
                "This is a saved sample. Click Refresh mail to load the entire Inbox without a limit.",
            )
        )
    groups = service.companies(store)
    group_for_sender = {sender: g for g in groups for sender in g["senders"]}
    company = next((g for g in groups if g["key"] == ss.get("inbox_company")), None)
    if company:
        st.button(T("← Вся почта", "← All mail"), key="company_back", on_click=close_company)
        st.subheader(company["name"])
        st.caption(T(
            f"Всего писем: {len(company['messages'])} · непрочитанных: {sum(bool(m.get('unread')) for m in company['messages'])}",
            f"Emails: {len(company['messages'])} · unread: {sum(bool(m.get('unread')) for m in company['messages'])}",
        ))
    sorts = {
        "newest": ("Письма: сначала новые", "Emails: newest first"),
        "company_count": ("Компании: больше всего писем", "Companies: most emails"),
        "company_name": ("Компании: по названию", "Companies: by name"),
        "company_recent": ("Компании: недавняя активность", "Companies: recent activity"),
    }
    order, select, clear, black = st.columns([3, 1.5, 1.5, 2], vertical_alignment="bottom")
    sort = order.selectbox(
        T("Показать", "Display"), list(sorts),
        format_func=lambda v: T(*sorts[v]), key="mail_display", on_change=reset_page,
    )
    st.text_input(
        T("Компания, email или тема письма", "Company, email or subject"),
        key="inbox_search", on_change=reset_page,
    )
    query = ss.get("inbox_search", "").strip().casefold()
    source = company["messages"] if company else messages
    # Include company names, including manually assigned names, in search.
    matching = [m for m in source if not query or query in " ".join([
        m.get("subject", ""), m.get("sender", ""), m.get("name", ""),
        group_for_sender.get(m["sender"], {}).get("name", ""),
    ]).casefold()]
    filtered = browse(matching, "", read_filter, "newest")
    white = {sender for sender, rule in store.rules().items() if rule["policy"] == "white"}
    black_senders = {sender for sender, rule in store.rules().items() if rule["policy"] == "black"}
    select.button(
        T("Выбрать всё", "Select all"), key="inbox_select_page", on_click=select_page,
        args=([m["uid"] for m in filtered if m["sender"] not in white],),
        disabled=not filtered, width="stretch",
        help=T("Все результаты поиска и фильтра, кроме белого списка.", "All matching emails across pages, except whitelist."),
    )
    clear.button(T("Снять выбор", "Clear selection"), key="inbox_clear", on_click=clear_selection,
                 disabled=not ss.inbox_selected, width="stretch")
    black.button(
        T("Выбрать чёрный список", "Select blacklist"), key="inbox_black", on_click=select_page,
        args=([m["uid"] for m in filtered if m["sender"] in black_senders and m["sender"] not in white],),
        disabled=not any(m["sender"] in black_senders and m["sender"] not in white for m in filtered), width="stretch",
    )
    per_page = st.selectbox(T("На странице", "Per page"), [25, 50, 100], key="inbox_size", on_change=reset_page)
    grouped = sort.startswith("company_") and not company
    matching_uids = {m["uid"] for m in filtered}
    rows = []
    if grouped:
        for g in groups:
            matched = [m for m in g["messages"] if m["uid"] in matching_uids]
            if matched:
                rows.append(dict(g, matched=matched))
        if sort == "company_name":
            rows.sort(key=lambda g: (g["name"].casefold(), g["key"]))
        elif sort == "company_count":
            rows.sort(key=lambda g: (-len(g["matched"]), g["name"].casefold()))
        else:
            rows.sort(key=lambda g: (-max(m["received"] for m in g["matched"]), g["name"].casefold()))
    else:
        rows = filtered
    page_count = max(1, math.ceil(len(rows) / per_page))
    ss.inbox_page = min(max(0, ss.get("inbox_page", 0)), page_count - 1)
    visible = rows[ss.inbox_page * per_page : (ss.inbox_page + 1) * per_page]
    white = {s for s, r in store.rules().items() if r["policy"] == "white"}
    selected = set(ss.inbox_selected)
    visible_uids = ({m["uid"] for g in visible for m in g["matched"]} if grouped else {m["uid"] for m in visible})
    st.caption(
        T(
            f"Найдено: {len(filtered)} · выбрано: {len(selected)} · вне страницы: {len(selected - visible_uids)}",
            f"Found: {len(filtered)} · selected: {len(selected)} · outside this page: {len(selected - visible_uids)}",
        )
    )
    local_selected = selected & {m["uid"] for m in source}
    action_uids = local_selected if company else selected
    if company and not action_uids:
        action_uids = {m["uid"] for m in filtered}
    st.caption(T(
        f"Действие затронет: {len(action_uids)} писем" + (" · текущий поиск и фильтр" if company and not local_selected else " · выбранные письма"),
        f"Action scope: {len(action_uids)} emails",
    ))
    if company:
        st.checkbox(T("Включить все письма компании, включая прочитанные", "Include all company emails, including read"),
                    key="company_all", on_change=reset_confirmation)
        if ss.get("company_all"):
            action_uids = {m["uid"] for m in source}
            st.caption(T(f"Действие затронет все {len(action_uids)} писем компании.", f"Action affects all {len(action_uids)} company emails."))
    modes = {
        "delete_only": (f"Удалить письма · {len(action_uids)}", f"Delete emails · {len(action_uids)}"),
        "unsubscribe_only": ("Отписаться", "Unsubscribe"),
        "unsubscribe_delete": (f"Отписаться и удалить · {len(action_uids)}", f"Unsubscribe and delete · {len(action_uids)}"),
    }
    for col, (mode, label) in zip(st.columns(3), modes.items()):
        if col.button(T(*label), key="inbox_review" if mode == "delete_only" else "inbox_" + mode,
                      disabled=not action_uids, width="stretch"):
            reset_confirmation()
            try:
                ss.inbox_confirmation = action_preview(store, scan["validity"], action_uids, mode, True)
                ss.inbox_confirmation["allow_white"] = False
            except service.MailError as exc:
                show_error(str(exc))
    confirmation = ss.get("inbox_confirmation")
    if confirmation:
        with st.container(border=True):
            st.subheader(T("Подтверждение действия", "Confirm action"))
            st.write(
                T(
                    f"В Корзину: {len(confirmation['targets'])} писем · рассылок для отписки: {len(confirmation['unsubs'])}.",
                    f"Trash: {len(confirmation['targets'])} emails · unsubscribe lists: {len(confirmation['unsubs'])}.",
                )
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            T("Тема", "Subject"): m["subject"],
                            T("Отправитель", "Sender"): m["sender"],
                            T("Дата", "Date"): date(m["received"]),
                        }
                        for m in confirmation["targets"]
                    ]
                ),
                hide_index=True,
                width="stretch",
                height=min(300, 40 + 35 * len(confirmation["targets"])),
            )
            if confirmation["mode"] != "delete_only":
                if confirmation["unsubs"]:
                    for unsub in confirmation["unsubs"]:
                        st.text(f"{unsub['sender']} · {unsub.get('list_id') or T('Рассылка', 'Mailing list')}")
                        st.caption(T("Автоматическая отписка" if unsub["one_click"] else "Потребуется ручная отписка",
                                     "Automatic unsubscribe" if unsub["one_click"] else "Manual unsubscribe needed"))
                else:
                    st.info(T("В выбранных письмах нет ссылок для отписки.", "No unsubscribe links in selected emails."))
            protected = bool(set(confirmation["senders"]) & white)
            if protected:
                st.warning(
                    T(
                        "В выборе есть письма отправителей из белого списка.",
                        "The selection includes emails from whitelisted senders.",
                    )
                )
                confirmation["allow_white"] = st.checkbox(
                    T(
                        "Я разрешаю действие для выбранных писем из белого списка",
                        "Allow this action for selected whitelisted emails",
                    ),
                    key="inbox_allow_white",
                    value=confirmation["allow_white"],
                )
            yes, no = st.columns(2)
            yes.button(
                T(
                    f"Подтвердить: {len(confirmation['targets'])} писем, {len(confirmation['unsubs'])} рассылок",
                    f"Move to Trash: {len(confirmation['targets'])} emails",
                ),
                key="inbox_confirm",
                type="primary",
                width="stretch",
                disabled=(protected and not confirmation["allow_white"]) or (not confirmation["targets"] and not confirmation["unsubs"]),
                on_click=confirm_deletion,
            )
            no.button(
                T("Отмена", "Cancel"),
                key="inbox_cancel",
                width="stretch",
                on_click=reset_confirmation,
            )

    item = next((m for m in messages if m["uid"] == ss.get("inbox_open_uid")), None)
    if item:
        with st.container(border=True):
            st.subheader(T("Просмотр письма", "Message reader"))
            st.text(item["subject"] or T("Без темы", "No subject"))
            st.text(
                f"{item['name']} <{item['sender']}> · {item['date'] or date(item['received'])}"
            )
            sender_company = group_for_sender.get(item["sender"])
            if sender_company:
                st.button(
                    T(f"Все письма {safe_label(sender_company['name'])} · {len(sender_company['messages'])}",
                      f"All emails from {safe_label(sender_company['name'])} · {len(sender_company['messages'])}"),
                    key="reader_company", on_click=open_company, args=(sender_company["key"],),
                )
            st.caption(
                T(
                    "Письмо не помечается прочитанным. Картинки и трекеры не загружаются.",
                    "Reading does not mark it as read. Images and trackers are not loaded.",
                )
            )
            content = ss.get("message_content")
            if content and content["uid"] == item["uid"]:
                if content["truncated"]:
                    st.caption(
                        T(
                            "Показано начало большого письма.",
                            "Showing the beginning of a large message.",
                        )
                    )
                with st.container(height=320, border=False):
                    st.text(
                        content["text"]
                        or T(
                            "В письме нет доступного текста.",
                            "No readable text in this email.",
                        )
                    )
            elif st.button(
                T("Загрузить текст письма", "Load message text"), key="inbox_read"
            ):
                start(
                    "read",
                    mail_provider.read_message,
                    store,
                    ss.password,
                    scan["validity"],
                    item,
                )
            st.button(
                T("Закрыть письмо", "Close message"),
                key="inbox_close",
                on_click=close_reader,
            )

    widths = [0.5, 4, 2.7, 1.7]
    with st.container(border=True, height=620, key="inbox_list"):
        header = st.columns(widths, vertical_alignment="center")
        for column, label in zip(
            header[1:],
            [
                T("Компания / открыть", "Company / open") if grouped else T("Тема / открыть", "Subject / open"),
                T("Письма", "Emails") if grouped else T("Отправитель", "Sender"),
                T("Получено", "Received"),
            ],
        ):
            column.caption(label)
        if not visible:
            st.info(
                T(
                    (
                        "Писем нет."
                        if not messages
                        else "По этим условиям писем не найдено."
                    ),
                    "No emails." if not messages else "No emails match these filters.",
                )
            )
        if grouped:
            for g in visible:
                cols = st.columns(widths, vertical_alignment="center")
                uids = [m["uid"] for m in g["matched"]]
                key = "inbox_group_" + g["key"]
                ss[key] = set(uids) <= selected
                cols[0].checkbox(T("Выбрать компанию", "Select company"), key=key,
                    label_visibility="collapsed", on_change=change_group_selection, args=(uids, key))
                cols[1].button(safe_label(g["name"]), key="company_open_" + g["key"],
                    on_click=open_company, args=(g["key"],), width="stretch")
                cols[2].text(T(f"{len(uids)} писем из {len(g['messages'])}", f"{len(uids)} of {len(g['messages'])} emails"))
                cols[2].caption(T(f"Непрочитанных: {sum(bool(m.get('unread')) for m in g['matched'])}",
                                    f"Unread: {sum(bool(m.get('unread')) for m in g['matched'])}"))
                cols[3].text(date(max(m["received"] for m in g["matched"])))
        else:
            for m in visible:
                cols = st.columns(widths, vertical_alignment="center")
                key = "inbox_uid_" + m["uid"]
                ss[key] = m["uid"] in selected
                cols[0].checkbox(
                    T("Выбрать письмо", "Select email"),
                    key=key,
                    label_visibility="collapsed",
                    on_change=change_selection,
                    args=(m["uid"], key),
                )
                subject = m["subject"] or T("Без темы", "No subject")
                prefix = ("● " if m.get("unread") else "") + (
                    "🔒 " if m["sender"] in white else ""
                )
                # Button labels support Markdown, including remote images. Treat
                # untrusted email subjects as literal text, as the reader does.
                label = re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~$])", r"\\\1", subject)
                cols[1].button(
                    prefix + ("**" + label + "**" if m.get("unread") else label),
                    key="inbox_open_" + m["uid"],
                    width="stretch",
                    on_click=open_message,
                    args=(m["uid"],),
                    help=label,
                )
                if m.get("snippet"):
                    cols[1].caption(m["snippet"][:120])
                cols[2].text(m["name"] or m["sender"])
                if m["name"]:
                    cols[2].caption(m["sender"])
                cols[3].text(date(m["received"]))
    prev, position, nxt = st.columns([1, 3, 1], vertical_alignment="center")
    prev.button(
        T("Назад", "Previous"),
        key="inbox_previous",
        on_click=turn_page,
        args=(-1,),
        disabled=ss.inbox_page == 0,
        width="stretch",
    )
    position.caption(
        T(
            f"Страница {ss.inbox_page + 1} из {page_count} · ● непрочитанное · 🔒 белый список",
            f"Page {ss.inbox_page + 1} of {page_count} · ● unread · 🔒 whitelist",
        )
    )
    nxt.button(
        T("Далее", "Next"),
        key="inbox_next",
        on_click=turn_page,
        args=(1,),
        disabled=ss.inbox_page + 1 >= page_count,
        width="stretch",
    )
