# HOA portal

A private, invite-only website for a homeowners association. It has six parts:

- **Document library.** The board keeps documents in a Dropbox folder. The portal picks them up automatically, and the subfolder a file is in decides who can see it.
- **Chatbot.** It answers questions from the documents the person asking is allowed to read, and cites its sources.
- **Email announcements.** The board can send them to homeowners, with a preview and an audit trail.
- **Asking management.** When the chatbot can't help, a homeowner can send the question to management. It goes as an email with the homeowner copied, so management answers by replying and the conversation carries on by email. The portal keeps no copy.
- **Duty calendar.** Recurring board and management duties, uploaded as a spreadsheet or added one at a time. Each duty's owners get reminder emails before it's due.
- **Amenity booking.** Households book the tennis court from a weekly calendar, under limits that keep things fair, and request the clubhouse, which management approves.

Nothing in this repository talks to a paid service unless you configure it. By default it uses an offline chatbot, offline embeddings and console email, so you can run it locally without any API keys.

## Roles

People join by invitation only. There is no public sign-up.

| Permission | Admin | Board | Management | Homeowner |
|---|:-:|:-:|:-:|:-:|
| Use the chatbot | ✓ | ✓ | ✓ | ✓ |
| Read documents shared with their group | all | ✓ | ✓ | ✓ |
| Add and manage documents (in Dropbox) | ✓ | ✓ | ✓ | |
| Invite homeowners | ✓ | ✓ | ✓ | |
| Invite any role, change roles, deactivate people | ✓ | | | |
| Send email announcements | ✓ | ✓ | | |
| Email a question to management | ✓ | ✓ | | ✓ |
| View the audit log | ✓ | ✓ | | |
| Book amenities | ✓ | ✓ | | ✓ |
| Approve requests, cancel any booking, close a space, see who booked | ✓ | | ✓ | |
| Manage the duty calendar | ✓ | ✓ | ✓ | |

The whole policy lives in `app/permissions.py`. Every route checks it on the server, so hiding a button is never the only protection. If someone opens a document they aren't allowed to see, they get a 404 rather than a 403, so the portal doesn't reveal that the document exists.

## How the chatbot stays inside permissions

1. When a document is added, its text is extracted (PDF, Word, Markdown or plain text), split into overlapping chunks and embedded. The chunks are stored in Postgres with pgvector.
2. When someone asks a question, the search query itself filters on the document's sharing table (`document_access.role IN (<the asker's role>)`). Text the asker can't read is never retrieved, so it is never sent to the model. `tests/test_chat.py` checks that a homeowner's question never puts board-only text in front of the model.
3. The permitted passages go to Claude as `document` blocks with citations turned on. The answer is shown with numbered links to the source document and page.
4. If nothing relevant is found, the model isn't called at all. The chatbot says it couldn't find an answer and offers to send the question to management. The same offer appears under every answer. That email copies only the asker's own questions, never the assistant's answers, which may quote documents management can't read.
5. Short follow-up questions are searched together with the previous question. Earlier turns are replayed as plain text only, so an earlier answer's sources can't leak into a later conversation.

The system prompt tells the model to answer only from the provided documents, to say so when they don't cover the question, and to ignore any instructions written inside a document.

## Amenity booking

People pick a free slot on a week calendar (`/reservations`). Every limit is per **household**: people whose accounts have the same unit (set on the People page or in the invite) share one set of limits. Someone with no unit counts as their own household. Limits also count across every space in a group, so if you add a second court, a household can't get round them by booking the other one.

The default rules, all configurable:

| Rule | Tennis court | Clubhouse |
|---|---|---|
| Spaces | one court | Clubhouse |
| Hours | 7:00 AM to 9:00 PM | 8:00 AM to 11:00 PM |
| Start times and lengths | every 30 minutes; 60 or 90 minutes | on the hour; 2 to 8 hours |
| How far ahead | up to 7 days | 14 to 180 days ahead |
| Upcoming bookings a household can hold | 2 | 1 |
| Per day / per week (Mon to Sun) | 1 / 3 | 1 / no limit |
| No back-to-back | at least 2 hours between a household's bookings | none |
| Late cancellations | cancelling within 2 hours of the start still counts toward the daily and weekly limits | no effect |
| Confirmation | instant | management approves or declines, with a note (fees, deposit, rules) emailed to the household |

Other homeowners see a slot only as "Booked"; management sees who booked it and what a clubhouse request is for. Management can cancel any booking (the household is emailed the reason) and close a space for maintenance or an HOA event. A closure ignores the limits but can't overlap existing bookings, so the people affected are always told. Two people pressing Book on the same slot at once can't both get it: on Postgres, booking takes a short advisory lock per amenity group. Every booking, request, decision, cancellation and closure is in the audit log.

Change the rules with two settings:

- `HOA_TIMEZONE` (default `America/New_York`): opening hours, days and weeks are in this time zone.
- `AMENITIES`: a JSON object of amenity groups. Each group has `label`, `spaces` (slug → name), optional `description` and `notes` (shown with the rules), and any of the rule keys `open`, `close`, `slot_minutes`, `min_minutes`, `max_minutes`, `book_ahead_days`, `min_notice_minutes`, `max_upcoming`, `max_per_day`, `max_per_week`, `gap_minutes`, `late_cancel_minutes`, `requires_approval` and `ask_purpose`. Leave a key out to use the default; set a limit to `null` for no limit. The full defaults are in `app/amenities.py`. Settings are checked at start-up, so a typo stops the app instead of being ignored.

With one court, a household can book once a day, so the 2-hour gap only starts to matter if you allow more. For example, two courts with two bookings a day each (the gap then stops a household playing back to back on either court):

```json
{"tennis": {"label": "Tennis courts", "spaces": {"court-1": "Court 1", "court-2": "Court 2"},
            "max_per_day": 2, "max_per_week": 4}}
```

## Duty calendar

The Duties page (`/duties`) lists recurring duties, such as reviewing the monthly financials or renewing the insurance policy, and what's due in the next 90 days.

- **Upload:** a CSV file with the columns `title`, `details`, `owners`, `first_due`, `repeat` and `remind_days`. The page links to a template and to a download of the current duties, so the spreadsheet can be edited and uploaded again. A row with the same title as an existing duty updates it, and nothing is ever deleted by an upload. If any row has a problem, nothing is saved and the page lists each bad row.
- **Owners:** email addresses, `board` or `management`, separated by semicolons. A role means every active account with that role at the time the reminder goes out. Homeowners can't be owners, so a duty can never email the whole community.
- **Repeats:** none, weekly, monthly, quarterly, twice a year or yearly. A monthly duty on the 31st falls on the last day of shorter months.
- **Reminders:** by default 7 days before and on the day, from 7 AM local time (`DUTY_REMINDER_HOUR`). The hourly background job sends them, one email per reminder with all the owners on it. Each reminder sent is recorded with a unique key, so it never goes out twice, even if the job runs twice at once. If the job misses a day, the reminder goes out late once instead of not at all.

## Architecture

```
FastAPI (server-rendered Jinja pages, tiny progressive-enhancement JS)
 ├─ app/security     sessions (hashed tokens in the DB), Argon2id passwords, CSRF, lockout
 ├─ app/permissions  roles → permissions; per-document sharing
 ├─ app/dropbox.py   read-only Dropbox client (list folder, download)
 ├─ app/services/dropbox_sync.py   Dropbox folder → document library (+ who can see what)
 ├─ app/storage      for portal uploads in dev: LocalEncryptedStorage (AES-256-GCM) | S3Storage
 ├─ app/ingest       text extraction + chunking
 ├─ app/embeddings   VoyageEmbedder | HashingEmbedder (offline)
 ├─ app/config.py    settings from env, AWS Secrets Manager, or .env
 ├─ app/rag          permission-filtered retrieval + Claude with citations | OfflineLLM
 ├─ app/email        transactional outbox → Console | SMTP | SES | Postmark
 ├─ app/amenities.py amenity groups and booking rules (from the AMENITIES setting)
 ├─ app/services     accounts, documents, chat, blasts, duties, escalation, reservations
 └─ app/worker.py    delivers queued email with retries, prunes expired sessions
Postgres + pgvector (HNSW cosine index). SQLite works for local dev and tests.
```

Some design notes:

- **Email.** Every email is first written to an `email_messages` outbox table in the same transaction as the action that caused it. The worker then delivers the messages with retries and exponential backoff, so a provider outage never loses an announcement or a message to management. Announcements go out as one message per recipient and include a `List-Unsubscribe` header.
- **Announcements.** The board writes a draft, previews it with an exact recipient count, and confirms by sending that count back. If the audience changed after the preview, the send is refused. Homeowners can opt out of announcements on their account page. A message marked as an official notice (for example, an annual meeting notice) still goes to people who opted out.
- **Audit log.** Sign-ins, invites, role changes, uploads, downloads, sharing changes, announcements, messages sent to management (that one was sent, not what it said) and bookings are all recorded and visible at `/admin/audit`.
- **Documents in Dropbox.** Dropbox is the source of truth. The board adds, replaces, renames, moves and deletes files there; the portal never changes anything in Dropbox (its Dropbox app has read-only permissions). The background worker lists the folder and brings the library in step: new files are read and indexed, changed files are re-indexed, and files that were deleted, or moved out of every shared folder, disappear from the portal and the chatbot. It checks every `DROPBOX_SYNC_MINUTES` when run as `python -m app.worker`, and every hour on AWS Lambda (`worker_schedule`, DEPLOY.md). A manager can also press **Check Dropbox now**. A download checks the folder the file is in at that moment, so moving a file to a more private folder stops its old audience downloading it straight away; the chatbot keeps answering from the old text until the next check, so press **Check Dropbox now** after moving a file to restrict it. If a changed file can't be read, it still takes its new folder's access, and its old text leaves the chatbot. Files are tracked by their Dropbox id, so renames and moves don't create duplicates. If the folder suddenly lists as empty, nothing is removed, since that more likely means lost access than a deliberate wipe. The portal keeps the extracted text for search, but not the files: a download is fetched from Dropbox after the portal's own permission check.
- **Who can see what.** The top-level subfolder decides it, using `DROPBOX_FOLDER_ROLES`. By default:

  | Folder (inside `DROPBOX_ROOT`, default `/HOA Portal`) | Who can see files in it |
  |---|---|
  | `Everyone` | Board, management, homeowners |
  | `Board and Management` | Board, management |
  | `Board` | Board |
  | `Management` | Management |

  Subfolders take the access of the folder they're in, and you can map deeper folders too (the deepest match wins). Files anywhere else are ignored. To change who can see a file, move it. Admins see everything. Dropbox's own sharing settings have no effect on the portal; only the folder matters.
- **Local development** doesn't need Dropbox: with `DOCUMENT_SOURCE=upload`, people upload through the portal and files are stored encrypted on disk (or in S3).
- **Chatbot limits.** Each person can ask `CHAT_DAILY_LIMIT_PER_USER` questions a day (default 20). The portal records the tokens of every answer in `chat_usage` and pauses the assistant for everyone once the month's estimated spend reaches `CHAT_MONTHLY_BUDGET_USD` (default $5), with a message pointing people to management. Admins see the month's spend on the chat page. The default model is Claude Haiku 5.5 with extended thinking off and answers capped at 1,000 tokens. The portal knows the prices of Claude Haiku, Sonnet and Opus 5.5 and how each turns thinking off (Opus 5.5 can't, so effort is the only control); for another model set `CHAT_PRICE_INPUT_PER_MTOK` and `CHAT_PRICE_OUTPUT_PER_MTOK`, or production refuses to start. Use a separate Anthropic Console key on prepaid credits with a workspace spend limit as the hard backstop (DEPLOY.md, step 3).
- **Search.** With `RETRIEVAL=keyword` (the AWS default) the chatbot finds passages with Postgres full-text search, so no embeddings account is needed. With `RETRIEVAL=vector` it uses embeddings (Voyage in production), which also match synonyms.
- **Secrets.** In production every secret lives in one AWS Secrets Manager secret, loaded at start-up (see `app/config.py`). Secret settings are hidden from logs and error pages.

## Running it locally

You need Python 3.12 or newer.

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m app.cli generate-key        # paste STORAGE_ENCRYPTION_KEY and SECRET_KEY into .env
alembic upgrade head
python -m app.cli seed                # demo users and five fictional sample documents
uvicorn app.main:app --reload
```

Open http://localhost:8000 and sign in as `admin@example.com`, `board@example.com`, `manager@example.com` or `owner@example.com`. The password is whatever you set in `SEED_DEMO_PASSWORD`, or `demo-password-123` if you left it blank. Emails are printed to the server log.

To run the full stack with Postgres and the worker instead, use `docker compose up --build`.

To use real Claude answers locally, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. For better search, also set `EMBEDDING_PROVIDER=voyage` and `VOYAGE_API_KEY`, then run `python -m app.cli reindex`.

### The sign-in page photo

Put a photo at `app/static/landing.jpg` (or `.jpeg`, `.png`, `.webp`) and the sign-in page shows it above the form. To credit the photographer, put one line such as `Photo: Jane Doe on Unsplash` in `app/static/landing-credit.txt`. With no photo file the page is just the sign-in card. Keep the photo around 1600 pixels wide and under about 300 KB so the page stays quick on phones. The page's security policy only allows images served by the portal itself, so the file has to live in `app/static`, not on another site.

### Tests

```
pytest -q                                                    # SQLite
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/hoa_test pytest -q   # Postgres + pgvector
ruff check . && ruff format --check .
```

CI runs both test configurations, checks the migrations and runs the linter on every pull request.

## Deploying

**[DEPLOY.md](DEPLOY.md)** sets the portal up on AWS Lambda with a free Neon Postgres database, for about $0.50 a month plus chatbot usage (capped). After a one-time setup, every merge to `main` deploys automatically through GitHub Actions, which signs in to AWS with GitHub's OIDC token instead of stored keys. The infrastructure is Terraform, in `infra/bootstrap` (run once by hand) and `infra/app` (applied by the deploy workflow).

The app refuses to start with `APP_ENV=production` unless the settings are production-safe. That means a real `SECRET_KEY`, secure cookies, Postgres, the Dropbox credentials (or upload storage with its encryption key), a real email provider, and the API key for the chatbot. The Dropbox setup is below; DEPLOY.md covers the rest.

### Documents (Dropbox)

1. In the Dropbox account that will hold the documents, create the folder `/HOA Portal` with the subfolders `Everyone`, `Board and Management`, `Board` and `Management`. A dedicated HOA Dropbox account works best; share the folder with board members so they can add files.
2. In the Dropbox App Console, create an app with **Scoped access** and **Full Dropbox** access. Under Permissions, enable only `files.metadata.read` and `files.content.read`. The portal can then read but never change anything. Full Dropbox access lets it reach that account's shared folder, which is another reason to connect a dedicated HOA account rather than someone's personal one.
3. Run `python -m app.cli dropbox-auth` on your own computer. It asks for the app key and secret, gives you a link to approve while signed in to that Dropbox account, and prints a refresh token.
4. `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET` and `DROPBOX_REFRESH_TOKEN` go in the secret (DEPLOY.md, step 4). Set `DROPBOX_ROOT` if you used another folder name.

### Running it somewhere else

The `Dockerfile` runs the same app as a long-lived server (App Runner, ECS, Fly.io, Render). There, run `alembic upgrade head` as a release step, run `python -m app.worker` as a second process, and set `EMAIL_SEND_INLINE=false`. Postgres needs the `vector` extension, which the first migration creates. Secrets still come from one Secrets Manager secret named by `AWS_SECRETS_MANAGER_SECRET_ID`, and environment variables override it.

## Security notes

- **Passwords.** Hashed with Argon2id, at least 12 characters. Sign-in locks for 15 minutes after 5 failures; resetting the password ends the lock. That stops one burst of guesses from keeping someone out, but not a stranger who keeps guessing, who can lock the account again right after a reset; the lock is per account, not per network address. A person gets at most 3 reset emails an hour, however often the form is sent. Login and reset pages don't reveal whether an email has an account. Changing your password ends any reset links already sent.
- **Sessions.** Random tokens stored hashed, with HttpOnly and SameSite=Lax cookies. Sessions end when a person is deactivated, their role changes or their password changes.
- **CSRF and headers.** Every form is CSRF-protected. Pages send a strict Content-Security-Policy with no inline scripts.
- **Files.** Checked by content, not just file name, and size-limited. Encrypted or scanned PDFs without text are listed but can't be searched.
- **Retention.** When a file is deleted in Dropbox, its text leaves the portal and the chatbot on the next check; the row is kept for the audit trail. Additions, permission changes from moves, and removals are all in the audit log. Invite and reset emails aren't kept once sent (their links work like passwords), and other delivered email is deleted after 30 days, except announcements, whose delivery counts their page shows.
- **Encryption at rest.** Files rely on Dropbox's encryption at rest. The extracted text lives in Postgres; Neon encrypts its storage at rest, as RDS does by default.
