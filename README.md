# HOA portal

A private, invite-only website for a homeowners association. It has four parts:

- **Document library.** Each document is visible only to the groups it is shared with, and files are encrypted at rest.
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
| Upload and manage documents | ✓ | ✓ | ✓ | |
| Invite homeowners | ✓ | ✓ | ✓ | |
| Invite any role, change roles, deactivate people | ✓ | | | |
| Send email announcements | ✓ | ✓ | | |
| Open tickets ("ask management") | ✓ | ✓ | | ✓ |
| Handle tickets (reply, internal notes, status) | ✓ | | ✓ | |
| View the audit log | ✓ | ✓ | | |

The whole policy lives in `app/permissions.py`. Every route checks it on the server, so hiding a button is never the only protection. If someone opens a document they aren't allowed to see, they get a 404 rather than a 403, so the portal doesn't reveal that the document exists. Board members can't see homeowners' tickets unless you set `BOARD_CAN_VIEW_TICKETS=true`.

## How the chatbot stays inside permissions

1. When a document is uploaded, its text is extracted (PDF, Word, Markdown or plain text), split into overlapping chunks and embedded. The chunks are stored in Postgres with pgvector.
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
 ├─ app/storage      LocalEncryptedStorage (AES-256-GCM envelope) | S3Storage (SSE-KMS)
 ├─ app/ingest       text extraction + chunking
 ├─ app/embeddings   VoyageEmbedder | HashingEmbedder (offline)
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
- **Storage.** Storage sits behind a small interface (`app/storage/base.py`), so another backend such as Dropbox or Google Drive could be added without touching the rest of the app.

## Running it locally

You need Python 3.12 or newer.

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m app.cli generate-key        # paste the output into LOCAL_STORAGE_KEY in .env
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

The app refuses to start with `APP_ENV=production` unless the settings are production-safe. That means a real `SECRET_KEY`, secure cookies, Postgres, encrypted storage that is properly configured, a real email provider and Voyage embeddings. A reasonable AWS setup:

1. **Database.** RDS for PostgreSQL 16 with the `vector` extension. The first migration runs `CREATE EXTENSION IF NOT EXISTS vector`, so the database user needs permission to do that once.
2. **Documents.** A private S3 bucket with Block Public Access turned on, plus a customer-managed KMS key. Set `STORAGE_BACKEND=s3`, `S3_BUCKET` and `S3_KMS_KEY_ID`. Give the app's role only `s3:GetObject`, `PutObject` and `DeleteObject` on the prefix, and `kms:Encrypt`, `Decrypt` and `GenerateDataKey` on the key.
3. **Email.** Amazon SES (`EMAIL_PROVIDER=ses`) or Postmark (`EMAIL_PROVIDER=postmark`, using its broadcast stream for announcements). Verify the sending domain and set up SPF, DKIM and DMARC before the first announcement.
4. **App.** Build the Dockerfile and run it on App Runner, ECS Fargate, Fly.io or Render behind HTTPS. Run `alembic upgrade head` as a release step. Run a second process with `python -m app.worker`, and set `EMAIL_SEND_INLINE=false`.
5. **Settings and secrets.** Set `HOA_NAME` to your association's name. Put `SECRET_KEY`, `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY` and the email credentials in your platform's secret store, never in the repository.
6. **First admin.** Run `python -m app.cli create-admin you@example.com` once against the production database. Invite everyone else from the People page.

## Security notes

- **Passwords.** Hashed with Argon2id, at least 12 characters. Sign-in locks for 15 minutes after 5 failures. Login and reset pages don't reveal whether an email has an account.
- **Sessions.** Random tokens stored hashed, with HttpOnly and SameSite=Lax cookies. Sessions end when a person is deactivated, their role changes or their password changes.
- **CSRF and headers.** Every form is CSRF-protected. Pages send a strict Content-Security-Policy with no inline scripts.
- **Uploads.** Checked by content, not just file name. Size-limited, and encrypted PDFs are rejected.
- **Retention.** Deleting a document removes its file and its chatbot index right away. The row is kept for the audit trail.
