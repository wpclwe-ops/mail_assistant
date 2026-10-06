# Mail Assistant · v3.7.0-test

## Gmail test build

Branch: **test/gmail**, based on main after the unified-Mail merge. Start screen selects iCloud or Gmail before showing provider-specific instructions and login. Gmail uses desktop OAuth with PKCE and a loopback callback, memory-only tokens, and the same Inbox UI for browsing, grouping, selection, unsubscribe, Trash, Undo, history and settings. No compose/send or permanent-delete endpoints. The gmail.modify permission is broader than the implemented features and technically permits sending.

**[Пошаговая инструкция входа Gmail](GMAIL_SETUP.md)**. Enable Gmail API, create an External/Testing Desktop OAuth client, add your Gmail as a test user, upload the client JSON, and follow the Google sign-in link. START.bat installs the required Google libraries. This local desktop flow runs on the same computer as your browser.

A local, bilingual tool for reviewing iCloud mail, unsubscribing from newsletters and moving selected messages to Trash.

## English

### What's new in 3.1

- Compact, scrollable company list. Search comes first and bulk actions stay above the list.
- Stable collapsible sidebar with navigation buttons, account below the Menu heading, and language selection in Settings (also available before sign-in).
- Each company has an **Emails** button: subjects, dates and senders, plus on-demand text reading. Email HTML is converted to plain text; images and tracking pixels are not loaded. Reading uses `BODY.PEEK` and does not mark messages as read; very large messages show only their beginning.
- Deletion is explicitly two-step: **Review and confirm → Move N emails to Trash**. Selecting “Delete only” does not execute an action. Empty advertising-filter results explain what to change; nothing is silently broadened to all mail.
- The app checks that each moved UID has disappeared from the source folder before reporting deletion as successful. Uncertain results stop the batch and are recorded, without automatic retry.
- The reported failure on the user's live iCloud account has not been reproduced against that account. Tests cover the UI, a fake mailbox, and real Python IMAP parsing against a local test server, not a live iCloud mailbox.

### Start and sign in

1. Install Python 3.10 or newer on Windows, with **Add Python to PATH** enabled.
2. Download/clone this repository. Keep all project files together.
3. Run `START.bat`. It creates `.venv`, installs dependencies and opens Streamlit locally.
4. On the welcome screen, enter your iCloud email address.
5. Open [Apple Account](https://account.apple.com/), then **Sign-In and Security → App-Specific Passwords**. Generate a password for this app. Apple requires two-factor authentication.
6. Paste that app-specific password into the welcome screen and connect. Your regular Apple password will not work.
7. Scan your inbox. Later sessions automatically load the last saved scan for the same account.

For macOS/Linux: create a virtual environment, install `requirements.txt`, then run `python -m streamlit run app.py --server.address 127.0.0.1`.

### Features

- Russian/English interface with 15 px typography and large controls.
- Dedicated welcome/sign-in screen; navigation for Mail, Whitelist, Blacklist, Company groups, History and Settings.
- Stage indicators and numeric progress for scanning, preview, deletion, unsubscribe and Undo. Controls are unavailable while a task runs.
- Conservative company grouping, company/email search and sorting by message count, latest message, company name or confirmed deletions in the last 30 days.
- Select several companies, select/deselect all, or select blacklisted senders.
- Three independent modes: **delete only**, **unsubscribe only**, **unsubscribe and delete**.
- Live preview of exact messages. All candidates start selected; untick any messages you want to keep. Advertising-only mode uses a subject heuristic, so review it carefully.
- One-click unsubscribe when supported. Other methods appear as manual web/email links. No unsubscribe email is sent automatically.
- Company status, last action, durable history and a result line without pop-up notifications.
- Undo the most recent deletion batch using the destination UIDs returned by iCloud.
- Whitelist protection: “Select all” skips protected companies; manually selecting one requires explicit permission. Protection is checked again before execution.
- Blacklist: a saved selection aid, not an automatic deletion rule.
- Merge selected sender addresses, split selected senders out of a group, and reset manual grouping rules.
- Account-scoped SQLite storage for scans, settings, history, lists, grouping, unsubscribe requests and Undo records. Language, scan limit, sorting and action preferences are remembered. Passwords stay only in app memory: a browser refresh keeps you signed in, while signing out or restarting the app requires signing in again. They are never written to disk.

### Important behavior

Only **INBOX** is scanned. A limited scan is a sample; preview searches all current inbox messages from the selected exact addresses. Counts can therefore differ. Scan again to discover new mail or changes made by another mail app.

An HTTP success means an **unsubscribe request was accepted**, not that a subscription is definitely cancelled. Separate mailing lists can require separate requests. “New after unsubscribe” counts unique messages found by scans whose server receipt time is after the latest accepted request. Limited scans can undercount; absence of new messages does not prove cancellation.

Deletion moves mail to Trash. The app never issues a broad `EXPUNGE`. It uses `UID MOVE`, or `UID COPY` plus targeted `UID EXPUNGE` when UIDPLUS is available. Undo is available only for confirmed moves with saved destination UIDs and matching UIDVALIDITY. If iCloud no longer has a message, does not return its new UID, or a connection breaks at an ambiguous point, automatic recovery may be unavailable: check Trash manually. Completed steps are recorded; partial results are not reported as full success. A crash can leave a `running` history entry: check mail before retrying.

On first upgrade from the old JSON version, **scan once again**. The old cache has no trustworthy account/UIDVALIDITY binding and cannot safely seed Undo or the new counters. Old JSON files are preserved; old unverified unsubscribe history is not imported as successful requests.

Local state (`mail_assistant.sqlite3`, its auxiliary files and old JSON caches) contains private mail metadata. Do not share those files. `.gitignore` excludes them; it cannot untrack a file already committed. Distribute the source code only. No AI service receives message contents.

### Update and test

In GitHub Desktop, **Fetch origin → Pull origin**, close the running app and terminal, then run `START.bat` again. Updates now include several Python modules; do not replace only `app.py`. Keep `.venv` and your local database.

Run `python -m unittest discover -s tests -v` for backend and Streamlit UI tests. The tests use a simulated IMAP server; they never touch a real mailbox. A live iCloud smoke test is still needed before relying on bulk actions.

## Русский

### Что изменилось в 3.1

- Компактный список компаний с собственной прокруткой. Поиск сверху, массовые действия — над списком.
- Сворачиваемое меню больше не исчезает во время операции. Навигация кнопками, email под заголовком «Меню», выбор языка — в настройках (и на экране до входа).
- Кнопка **«Письма»** у каждой компании: темы, даты, отправители и загрузка текста по запросу. HTML превращается в обычный текст, картинки и трекеры не загружаются. Просмотр не помечает письмо прочитанным; для очень больших писем показывается начало.
- Удаление в два явных шага: **«Просмотреть и подтвердить → Переместить в Корзину: N писем»**. Выбор режима «Только удалить» сам по себе ничего не удаляет. Если фильтр рекламы не нашёл кандидатов, появляется объяснение; программа сама не переключается на удаление всех писем.
- Перед отчётом об успешном удалении проверяется исчезновение UID из исходной папки. Неоднозначный результат останавливает пачку и записывается в историю без автоматического повтора.
- Сообщённый сбой на реальном аккаунте iCloud пока не воспроизведён именно с этим аккаунтом. Проверки охватывают интерфейс, имитацию ящика и настоящий IMAP-парсер Python с локальным тестовым сервером, а не реальный iCloud.

### Запуск и вход

1. Установи Python 3.10+ для Windows с галочкой **Add Python to PATH**.
2. Скачай/клонируй репозиторий. Все файлы проекта должны лежать вместе.
3. Запусти `START.bat`: он создаст `.venv`, установит зависимости и откроет приложение локально.
4. На стартовом экране введи email iCloud.
5. Открой [аккаунт Apple](https://account.apple.com/) → **Вход и безопасность → Пароли приложений**. Создай пароль для приложения. Нужна двухфакторная аутентификация.
6. Вставь пароль приложения и подключись. Обычный пароль Apple не подходит.
7. Просканируй почту. При следующем входе загрузится сохранённая выборка этого аккаунта.

### Что умеет

- Русский/English, шрифт 15 px, крупные кнопки, отдельный экран входа.
- Меню: Почта, Белый список, Чёрный список, Объединение компаний, История, Настройки.
- Этапы работы и числовой прогресс; блокировка управления во время операции.
- Строгая группировка, поиск по компании/email, сортировки по количеству писем, последнему письму, названию и подтверждённым удалениям за 30 дней.
- Выбор нескольких компаний, «Выбрать всё / Снять всё», выбор чёрного списка.
- Три режима: **удалить**, **отписаться**, **отписаться и удалить**.
- Предпросмотр: все письма выбраны, с нужных можно снять галочки. Режим «вероятная реклама» оценивает тему приблизительно.
- Автоматическая one-click отписка; остальные способы — ручные ссылки. Письма для отписки автоматически не отправляются.
- Статусы компаний, последнее действие, история, результат строкой без всплывающих окон.
- Возврат последней удалённой пачки из Корзины по сохранённым новым UID.
- Белый список: защита от массового выбора, отдельное разрешение для ручного действия.
- Чёрный список: сохранённый список для быстрого выбора, без автоматического удаления.
- Ручное объединение, разделение выбранных отправителей и возврат к автоматической группировке.
- Локальная SQLite-база отдельно по аккаунтам. Язык, сортировка, объём сканирования и режим запоминаются. Пароль хранится только в памяти приложения: после обновления страницы вход сохраняется, а после выхода или перезапуска приложения нужно войти снова. На диск пароль не записывается.

### Что нужно учитывать

Сканируются только **Входящие (INBOX)**. Ограниченное сканирование даёт выборку, а предпросмотр ищет все текущие письма выбранных точных адресов во Входящих. Поэтому числа могут различаться. Новые письма и изменения из других приложений появляются после нового сканирования.

«Запрос принят» не означает гарантированную отписку: у компании может быть несколько независимых рассылок. Счётчик новых писем использует время получения сервером после последнего принятого запроса и только письма, найденные сканированиями. При ограниченной выборке число может быть неполным.

Удаление перемещает письма в Корзину. Общий `EXPUNGE` не используется. Возврат возможен только при подтверждённом перемещении, сохранённых новых UID и совпадающем UIDVALIDITY. После очистки Корзины, отсутствия нового UID в ответе iCloud или неоднозначного сетевого сбоя может потребоваться ручной возврат. Частичные результаты сохраняются в истории. Запись `running` после перезапуска может означать прерванную операцию — проверь почту перед повтором.

После перехода со старой JSON-версии **нужно один раз пересканировать почту**: старый кэш не привязан надёжно к аккаунту и UIDVALIDITY. Старые файлы сохраняются, но их неподтверждённая история отписок не импортируется как успешные запросы.

База, вспомогательные SQLite-файлы и старый кэш содержат личные данные. Не передавай их друзьям и не загружай в GitHub. Они добавлены в `.gitignore`. Друзьям передавай только исходный код. Содержимое писем не передаётся ИИ-сервисам.

### Обновление

В GitHub Desktop: **Fetch origin → Pull origin**. Закрой приложение и его терминал, затем запусти `START.bat`. Теперь нужно обновлять все файлы проекта, а не только `app.py`. `.venv` и локальную базу сохраняй.

Проверки: `python -m unittest discover -s tests -v`. Они используют имитацию IMAP и не меняют настоящую почту. Перед массовым применением нужен контрольный запуск на реальном iCloud.
