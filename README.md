# HOA portal

A private, invite-only website for a homeowners association. It has four parts:

- **Document library.** The board keeps documents in a Dropbox folder. The portal picks them up automatically, and the subfolder a file is in decides who can see it.
- **Chatbot.** It answers questions from the documents the person asking is allowed to read, and cites its sources.
- **Email announcements.** The board can send them to homeowners, with a preview and an audit trail.
- **Escalation.** When the chatbot can't help, a homeowner can send the question to management as a ticket, and management can reply.

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
| Open tickets ("ask management") | ✓ | ✓ | | ✓ |
| Handle tickets (reply, internal notes, status) | ✓ | | ✓ | |
| View the audit log | ✓ | ✓ | | |

The whole policy lives in `app/permissions.py`. Every route checks it on the server, so hiding a button is never the only protection. If someone opens a document they aren't allowed to see, they get a 404 rather than a 403, so the portal doesn't reveal that the document exists. Board members can't see homeowners' tickets unless you set `BOARD_CAN_VIEW_TICKETS=true`.

## How the chatbot stays inside permissions

1. When a document is added, its text is extracted (PDF, Word, Markdown or plain text), split into overlapping chunks and embedded. The chunks are stored in Postgres with pgvector.
2. When someone asks a question, the search query itself filters on the document's sharing table (`document_access.role IN (<the asker's role>)`). Text the asker can't read is never retrieved, so it is never sent to the model. `tests/test_chat.py` checks that a homeowner's question never puts board-only text in front of the model.
3. The permitted passages go to Claude as `document` blocks with citations turned on. The answer is shown with numbered links to the source document and page.
4. If nothing relevant is found, the model isn't called at all. The chatbot says it couldn't find an answer and offers to send the question to management. The same offer appears under every answer.
5. Short follow-up questions are searched together with the previous question. Earlier turns are replayed as plain text only, so an earlier answer's sources can't leak into a later conversation.

The system prompt tells the model to answer only from the provided documents, to say so when they don't cover the question, and to ignore any instructions written inside a document.

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
 ├─ app/services     accounts, documents, chat, blasts, tickets
 └─ app/worker.py    delivers queued email with retries, prunes expired sessions
Postgres + pgvector (HNSW cosine index). SQLite works for local dev and tests.
```

Some design notes:

- **Email.** Every email is first written to an `email_messages` outbox table in the same transaction as the action that caused it. The worker then delivers the messages with retries and exponential backoff, so a provider outage never loses an announcement or a ticket notification. Announcements go out as one message per recipient and include a `List-Unsubscribe` header.
- **Announcements.** The board writes a draft, previews it with an exact recipient count, and confirms by sending that count back. If the audience changed after the preview, the send is refused. Homeowners can opt out of announcements on their account page. A message marked as an official notice (for example, an annual meeting notice) still goes to people who opted out.
- **Audit log.** Sign-ins, invites, role changes, uploads, downloads, sharing changes, announcements and ticket activity are all recorded and visible at `/admin/audit`.
- **Documents in Dropbox.** Dropbox is the source of truth. The board adds, replaces, renames, moves and deletes files there; the portal never changes anything in Dropbox (its Dropbox app has read-only permissions). Every `DROPBOX_SYNC_MINUTES` the worker lists the folder and brings the library in step: new files are read and indexed, changed files are re-indexed, and files that were deleted, or moved out of every shared folder, disappear from the portal and the chatbot. A manager can also press **Check Dropbox now**. Files are tracked by their Dropbox id, so renames and moves don't create duplicates. If the folder suddenly lists as empty, nothing is removed, since that more likely means lost access than a deliberate wipe. The portal keeps the extracted text for search, but not the files: a download is fetched from Dropbox after the portal's own permission check.
- **Who can see what.** The top-level subfolder decides it, using `DROPBOX_FOLDER_ROLES`. By default:

  | Folder (inside `DROPBOX_ROOT`, default `/HOA Portal`) | Who can see files in it |
  |---|---|
  | `Everyone` | Board, management, homeowners |
  | `Board and Management` | Board, management |
  | `Board` | Board |
  | `Management` | Management |

  Subfolders take the access of the folder they're in, and you can map deeper folders too (the deepest match wins). Files anywhere else are ignored. To change who can see a file, move it. Admins see everything. Dropbox's own sharing settings have no effect on the portal; only the folder matters.
- **Local development** doesn't need Dropbox: with `DOCUMENT_SOURCE=upload`, people upload through the portal and files are stored encrypted on disk (or in S3).
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

### Tests

```
pytest -q                                                    # SQLite
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/hoa_test pytest -q   # Postgres + pgvector
ruff check . && ruff format --check .
```

CI runs both test configurations, checks the migrations and runs the linter on every pull request.

## Deploying

The app refuses to start with `APP_ENV=production` unless the settings are production-safe. That means a real `SECRET_KEY`, secure cookies, Postgres, the Dropbox credentials (or upload storage with its encryption key), a real email provider, and the API keys for the chatbot and embeddings. A reasonable AWS setup:

1. **Database.** RDS for PostgreSQL 16 with the `vector` extension. The first migration runs `CREATE EXTENSION IF NOT EXISTS vector`, so the database user needs permission to do that once.
2. **Documents (Dropbox).**
   1. In the Dropbox account that will hold the documents, create the folder `/HOA Portal` with the subfolders `Everyone`, `Board and Management`, `Board` and `Management`. A dedicated HOA Dropbox account works best; share the folder with board members so they can add files.
   2. In the Dropbox App Console, create an app with **Scoped access** and **Full Dropbox** access. Under Permissions, enable only `files.metadata.read` and `files.content.read`. The portal can then read but never change anything. Full Dropbox access lets it reach that account's shared folder, which is another reason to connect a dedicated HOA account rather than someone's personal one.
   3. Run `python -m app.cli dropbox-auth` on your own computer. It asks for the app key and secret, gives you a link to approve while signed in to that Dropbox account, and prints a refresh token.
   4. Set `DOCUMENT_SOURCE=dropbox` (and `DROPBOX_ROOT` if you used another folder name). `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET` and `DROPBOX_REFRESH_TOKEN` go in the secret (step 5). Run the worker so the folder is checked on a schedule, or run `python -m app.cli dropbox-sync` once to import everything straight away.

3. **Email.** Amazon SES (`EMAIL_PROVIDER=ses`) or Postmark (`EMAIL_PROVIDER=postmark`, using its broadcast stream for announcements). Verify the sending domain and set up SPF, DKIM and DMARC before the first announcement.
4. **App.** Build the Dockerfile and run it on App Runner, ECS Fargate, Fly.io or Render behind HTTPS. Run `alembic upgrade head` as a release step. Run a second process with `python -m app.worker`, and set `EMAIL_SEND_INLINE=false`.
5. **Secrets (AWS Secrets Manager).** Create one secret, for example `hoa-portal/production`, whose value is a JSON object:

   ```json
   {
     "SECRET_KEY": "…",
     "DATABASE_URL": "postgresql+psycopg://…",
     "DROPBOX_APP_KEY": "…",
     "DROPBOX_APP_SECRET": "…",
     "DROPBOX_REFRESH_TOKEN": "…",
     "ANTHROPIC_API_KEY": "…",
     "VOYAGE_API_KEY": "…"
   }
   ```

   Add `SMTP_PASSWORD` or `POSTMARK_SERVER_TOKEN` if you use those. Give the app's role `secretsmanager:GetSecretValue` on that one secret (and `kms:Decrypt` if the secret uses a customer-managed key). Then set only non-secret settings in the environment: `APP_ENV=production`, `AWS_SECRETS_MANAGER_SECRET_ID=hoa-portal/production`, `AWS_REGION`, `HOA_NAME`, `BASE_URL`, `SESSION_COOKIE_SECURE=true`, `DOCUMENT_SOURCE=dropbox`, `EMBEDDING_PROVIDER=voyage`, `LLM_PROVIDER=anthropic`, `EMAIL_PROVIDER`, `EMAIL_FROM` and `EMAIL_SEND_INLINE=false`. Environment variables override the secret, which is handy for local testing. The secret is read once at start-up, so restart the app after rotating a value.
6. **First admin.** Run `python -m app.cli create-admin you@example.com` once against the production database. Invite everyone else from the People page.

## Security notes

- **Passwords.** Hashed with Argon2id, at least 12 characters. Sign-in locks for 15 minutes after 5 failures. Login and reset pages don't reveal whether an email has an account.
- **Sessions.** Random tokens stored hashed, with HttpOnly and SameSite=Lax cookies. Sessions end when a person is deactivated, their role changes or their password changes.
- **CSRF and headers.** Every form is CSRF-protected. Pages send a strict Content-Security-Policy with no inline scripts.
- **Files.** Checked by content, not just file name, and size-limited. Encrypted or scanned PDFs without text are listed but can't be searched.
- **Retention.** When a file is deleted in Dropbox, its text leaves the portal and the chatbot on the next check; the row is kept for the audit trail. Additions, permission changes from moves, and removals are all in the audit log.
- **Encryption at rest.** Files rely on Dropbox's encryption at rest. The extracted text and embeddings live in Postgres, so use an encrypted RDS instance (the default for new instances).
