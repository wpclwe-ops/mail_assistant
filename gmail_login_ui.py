"""Provider-specific Google setup and sign-in, kept outside the mail UI."""
from pathlib import Path
import streamlit as st
from gmail_auth import OAuthAttempt, validate_client_config
from mail_service import MailError


def render(start, T, show_error):
    st.subheader(T("Подключение Gmail", "Connect Gmail"))
    st.write(T(
        "Вход выполняется на странице Google. Пароль Google здесь вводить не нужно.",
        "Sign in on Google's page. Do not enter your Google password here.",
    ))
    with st.expander(T("Первая настройка Gmail — пошагово", "First Gmail setup — step by step"), expanded=True):
        st.markdown(T(
            "1. Открой **Google Cloud Console** по кнопке ниже и создай проект **Mail Assistant**.\n"
            "2. В **APIs & Services → Library** найди **Gmail API** и нажми **Enable**.\n"
            "3. Открой **Google Auth platform → Branding → Get started**. Название — Mail Assistant, email поддержки и контакта — твой. Для личного Gmail выбери аудиторию **External**.\n"
            "4. В **Audience** оставь **Testing** и в **Test users → Add users** добавь адрес Gmail, который подключишь.\n"
            "5. В **Data Access → Add or remove scopes** добавь `https://www.googleapis.com/auth/gmail.modify`.\n"
            "6. В **Clients → Create client** выбери **Desktop app**. Создай клиент и скачай его **JSON**. Тип Web application здесь не подходит.\n"
            "7. Загрузи этот JSON ниже. Альтернатива: положи его рядом с START.bat под именем **gmail_credentials.json**.\n"
            "8. Нажми **Войти через Google**, затем **Открыть вход Google**. Выбери нужный аккаунт, разреши доступ и вернись в приложение.",
            "1. Open **Google Cloud Console** and create a **Mail Assistant** project.\n"
            "2. Enable **Gmail API** in **APIs & Services → Library**.\n"
            "3. Configure **Google Auth platform → Branding**. For personal Gmail choose **External** audience.\n"
            "4. Keep **Testing** in **Audience**; add your Gmail address under **Test users**.\n"
            "5. Add `https://www.googleapis.com/auth/gmail.modify` under **Data Access**.\n"
            "6. Under **Clients**, create a **Desktop app** OAuth client and download its JSON.\n"
            "7. Upload the JSON below, or put it next to START.bat as **gmail_credentials.json**.\n"
            "8. Click **Sign in with Google**, then **Open Google sign-in**. Choose your account, grant access and return here.",
        ))
        st.link_button(T("Открыть Google Cloud Console", "Open Google Cloud Console"), "https://console.cloud.google.com/")
        st.link_button(T("Официальная инструкция Google", "Official Google instructions"), "https://developers.google.com/workspace/gmail/api/quickstart/python")
        st.caption(T(
            "Если Google покажет предупреждение о непроверенном приложении, продолжай только для собственного проекта Mail Assistant. Ошибка access_denied обычно означает, что Gmail не добавлен в Test users. В режиме Testing Google ограничивает срок refresh-токена; после его истечения потребуется новый вход.",
            "For an unverified-app warning, continue only for your own Mail Assistant project. access_denied often means your address is not a Test user. Testing mode limits refresh-token lifetime; reauthorize when needed.",
        ))
    upload = st.file_uploader(T("JSON OAuth-клиента Google", "Google OAuth client JSON"), type=["json"], key="gmail_client_upload")
    config = None
    try:
        if upload:
            if upload.size > 65536:
                raise MailError("gmail_config")
            config = validate_client_config(upload.getvalue())
        else:
            path = Path(__file__).with_name("gmail_credentials.json")
            if path.exists():
                if path.stat().st_size > 65536:
                    raise MailError("gmail_config")
                config = validate_client_config(path.read_bytes())
    except (MailError, OSError):
        show_error("gmail_config")
    if config:
        st.success(T("OAuth-клиент загружен. Можно войти в Google.", "OAuth client loaded. Ready to sign in."))
    else:
        st.info(T("Для первого входа загрузи JSON, скачанный на шаге 6.", "Upload the JSON from step 6 to sign in."))
    st.caption(T(
        "Приложение читает Входящие, переносит письма в Корзину и возвращает их. Отправки писем нет. Разрешение gmail.modify у Google шире этих функций и технически допускает отправку. OAuth-токены остаются в памяти, не сохраняются на диск. После перезапуска START.bat нужен новый вход.",
        "The app reads Inbox, moves emails to Trash and restores them. No sending feature. Google's gmail.modify scope is broader and technically permits sending. OAuth tokens stay in memory; restarting START.bat requires sign-in again.",
    ))
    if st.button(T("Войти через Google", "Sign in with Google"), key="gmail_signin", type="primary", width="stretch", disabled=not config):
        try:
            attempt = OAuthAttempt(config)
            st.session_state.gmail_attempt = attempt
            start("gmail_login", attempt.authenticate)
        except MailError as exc:
            show_error(str(exc))
