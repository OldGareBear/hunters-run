"""What the AWS Lambda deployment relies on: usage limits, keyword search, the
Lambda entry point, background hand-off and large downloads."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.pool import NullPool

from app import background
from app.config import get_settings
from app.db import make_engine
from app.dropbox import DropboxClient
from app.models import ChatUsage, Conversation, Document, User
from app.permissions import Role
from app.rag.llm import Answer, Citation
from app.rag.retrieval import retrieve
from app.services import chat as chat_service
from app.services import chat_limits
from app.services import documents as doc_service

POOL = "Pool rules. The pool is open from 10 AM to 8 PM daily. Guests must be accompanied."
FENCE = "Architectural rules. Fences need board approval and may be at most four feet high."
SECRET = "Executive session. The board discussed fencing bids and the reserve fund."


class FakeLLM:
    def answer(self, *, question, history, sources):
        return Answer(
            "Here.[[src:0]]",
            [Citation(0, sources[0].text[:20])],
            model="claude-haiku-5-5",
            input_tokens=2_000_000,  # big numbers so a few calls cross a small budget
            output_tokens=200_000,
        )


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(chat_service, "get_llm", lambda: FakeLLM())


@pytest.fixture
def docs(db, make_user):
    admin = make_user(Role.ADMIN, "admin@example.com")
    for title, text, roles in [
        ("Pool rules", POOL, ["board", "management", "homeowner"]),
        ("Architectural rules", FENCE, ["board", "management", "homeowner"]),
        ("Executive minutes", SECRET, ["board"]),
    ]:
        doc_service.create_document(
            db,
            uploader=admin,
            title=title,
            description="",
            filename=f"{title}.txt",
            data=text.encode(),
            declared_type="text/plain",
            roles=roles,
        )
    db.commit()


@pytest.fixture
def keyword(monkeypatch):
    monkeypatch.setattr(get_settings(), "retrieval", "keyword")


def _owner(db):
    return db.scalar(select(User).where(User.role == Role.HOMEOWNER.value))


# --- keyword search -----------------------------------------------------------
def test_keyword_search_finds_stems_and_respects_permissions(db, make_user, docs, keyword):
    owner = make_user(Role.HOMEOWNER, "owner@example.com")
    found = retrieve(db, owner, "Can I put up a fence?")
    assert [s.title for s in found] == ["Architectural rules"]  # "fence" matches "Fences"
    assert all("Executive" not in s.title for s in retrieve(db, owner, "fencing bids board"))
    board = make_user(Role.BOARD, "board@example.com")
    assert "Executive minutes" in [s.title for s in retrieve(db, board, "fencing bids")]
    assert retrieve(db, owner, "the and of") == []  # only stop words


# --- usage limits -------------------------------------------------------------
def test_daily_limit_per_person(db, make_user, docs, llm, keyword, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_daily_limit_per_user", 2)
    monkeypatch.setattr(get_settings(), "chat_monthly_budget_usd", 0)
    owner = make_user(Role.HOMEOWNER, "owner@example.com")
    conv = Conversation(user_id=owner.id)
    db.add(conv)
    db.flush()
    chat_service.ask(db, owner, conv, "pool hours?")
    chat_service.ask(db, owner, conv, "nothing matches this")  # no sources still counts
    assert chat_limits.status(db, owner).remaining_today == 0
    with pytest.raises(chat_service.LimitReached, match="daily limit"):
        chat_service.ask(db, owner, conv, "fence height?")
    # Deleting the conversation doesn't reset the count.
    db.delete(conv)
    db.commit()
    assert chat_limits.status(db, owner).remaining_today == 0
    # Yesterday's questions don't count today.
    for row in db.scalars(select(ChatUsage)):
        row.created_at = datetime.now(UTC) - timedelta(days=1, hours=1)
    db.commit()
    assert chat_limits.status(db, owner).remaining_today == 2


def test_monthly_budget_pauses_the_assistant(db, make_user, docs, llm, keyword, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "chat_daily_limit_per_user", 0)
    monkeypatch.setattr(s, "chat_monthly_budget_usd", 0.50)
    owner = make_user(Role.HOMEOWNER, "owner@example.com")
    conv = Conversation(user_id=owner.id)
    db.add(conv)
    db.flush()
    # Each fake answer: 2M input x $0.10 + 0.2M output x $0.50 per million = $0.30.
    chat_service.ask(db, owner, conv, "pool hours?")
    assert db.scalar(select(ChatUsage.cost_micros)) == 300_000
    chat_service.ask(db, owner, conv, "pool guests?")
    st = chat_limits.status(db, owner)
    assert st.over_budget and round(st.month_spend_usd, 2) == 0.60
    with pytest.raises(chat_service.LimitReached, match="paused until"):
        chat_service.ask(db, owner, conv, "fence?")


def test_chat_page_shows_the_pause(client_for, db, docs, llm, keyword, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_monthly_budget_usd", 0.01)
    c = client_for(Role.HOMEOWNER)
    db.add(ChatUsage(user_id=None, cost_micros=20_000))
    db.commit()
    page = c.get("/chat").text
    assert "paused until" in page and 'name="question"' not in page
    r = c.post_form("/chat", {"question": "pool?"})
    assert r.status_code == 303
    assert db.scalar(select(Conversation).where(Conversation.user_id == c.user.id)) is None


def test_limit_reached_while_starting_a_chat_is_a_message(client_for, db, monkeypatch):
    def ask(*_a, **_k):  # another tab used the last question after the route's check
        raise chat_service.LimitReached("You've asked 20 questions today")

    monkeypatch.setattr(chat_service, "ask", ask)
    c = client_for(Role.HOMEOWNER)
    r = c.post_form("/chat", {"question": "pool?"})
    assert r.status_code == 303 and r.headers["location"] == "/chat"
    assert db.scalar(select(Conversation)) is None


def test_admins_see_the_month_spend(client_for, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_monthly_budget_usd", 5)
    db.add(ChatUsage(user_id=None, cost_micros=1_230_000))
    db.commit()
    assert "$1.23 of $5.00" in client_for(Role.ADMIN).get("/chat").text
    assert "$1.23" not in client_for(Role.HOMEOWNER).get("/chat").text


# --- Lambda entry point and background hand-off -------------------------------
def test_lambda_tasks_dispatch(monkeypatch):
    from app import lambda_handler

    calls = []
    monkeypatch.setattr(lambda_handler, "_worker", lambda: calls.append("worker") or "ok")
    monkeypatch.setattr(lambda_handler, "_dropbox_sync", lambda a=None: calls.append(a) or "ok")
    assert lambda_handler.handler({"task": "worker"}, None) == {"task": "worker", "result": "ok"}
    lambda_handler.handler({"task": "dropbox-sync", "actor_id": "7"}, None)
    assert calls == ["worker", 7]
    with pytest.raises(ValueError):
        lambda_handler.handler({"task": "rm -rf"}, None)


def test_lambda_serves_http_through_mangum(monkeypatch):
    from app import lambda_handler

    monkeypatch.setattr(lambda_handler, "_http_handler", None)
    event = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/login",
        "rawQueryString": "",
        "headers": {"host": "example.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {
            "http": {"method": "GET", "path": "/login", "sourceIp": "203.0.113.9"},
            "domainName": "example.execute-api.us-east-1.amazonaws.com",
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }
    res = lambda_handler.handler(event, None)
    assert res["statusCode"] == 200 and "Sign in" in res["body"]


def test_background_work_goes_to_the_tasks_function(monkeypatch):
    sent = []

    class FakeLambda:
        def invoke(self, **kw):
            sent.append(kw)

    monkeypatch.setattr(get_settings(), "tasks_function_name", "hoa-portal-tasks")
    monkeypatch.setattr(background, "_lambda_client", lambda: FakeLambda())

    class BG:
        def add_task(self, fn, *a, **kw):
            fn(*a, **kw)

    background.run_later(BG(), "dropbox-sync", lambda **_: pytest.fail("ran locally"), actor_id=3)
    assert sent[0]["FunctionName"] == "hoa-portal-tasks"
    assert sent[0]["InvocationType"] == "Event"
    assert json.loads(sent[0]["Payload"]) == {"task": "dropbox-sync", "actor_id": 3}


def test_null_pool_for_lambda():
    assert isinstance(make_engine("postgresql+psycopg://u:p@h/db", null_pool=True).pool, NullPool)


# --- large downloads ----------------------------------------------------------
def test_dropbox_temporary_link():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 14400})
        assert request.url.path.endswith("/files/get_temporary_link")
        assert json.loads(request.content) == {"path": "id:abc"}
        meta = {"id": "id:abc", "name": "a.pdf", "path_lower": "/hoa portal/board/a.pdf"}
        return httpx.Response(
            200, json={"metadata": meta, "link": "https://dl.dropboxusercontent.com/x"}
        )

    client = DropboxClient(
        app_key="k", app_secret="s", refresh_token="r", transport=httpx.MockTransport(handler)
    )
    assert client.temporary_link("id:abc") == "https://dl.dropboxusercontent.com/x"
    link, now = client.temporary_link_with_meta("id:abc")
    assert now.path_lower == "/hoa portal/board/a.pdf"  # where the file is now


def test_big_dropbox_files_redirect(client_for, db, docs, monkeypatch):
    import app.dropbox as dbx

    monkeypatch.setattr(get_settings(), "download_redirect_over_mb", 4)

    def meta(file_id):
        path = "/hoa portal/everyone/x.pdf"
        return dbx.DropboxFile(file_id, "x.pdf", path, path, "", 10)

    class FakeDropbox:
        def temporary_link_with_meta(self, file_id):
            return f"https://dl.dropboxusercontent.com/{file_id}", meta(file_id)

        def download_with_meta(self, file_id):
            return b"small file", meta(file_id)

    monkeypatch.setattr(get_settings(), "dropbox_root", "/HOA Portal")
    monkeypatch.setattr(dbx, "get_dropbox", lambda for_request=False: FakeDropbox())
    big, small = db.scalars(select(Document).order_by(Document.id)).all()[:2]
    for doc, size in ((big, 9 * 1024 * 1024), (small, 1000)):
        doc.source, doc.source_id, doc.size_bytes = "dropbox", f"id:{doc.id}", size
    db.commit()
    c = client_for(Role.HOMEOWNER)
    r = c.get(f"/documents/{big.id}/download", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"https://dl.dropboxusercontent.com/id:{big.id}"
    r = c.get(f"/documents/{small.id}/download", follow_redirects=False)
    assert r.status_code == 200 and r.content == b"small file"


def test_lambda_environment_passes_production_checks(monkeypatch):
    """The settings Terraform gives the Lambda, plus the secret, make a valid production config."""
    import re
    from pathlib import Path

    from app.config import Settings

    tf = (Path(__file__).parents[1] / "infra/app/main.tf").read_text()
    block = tf.split("  env_all = {", 1)[1].split("\n  }", 1)[0]
    env = dict(re.findall(r'^\s*([A-Z_]+)\s*=\s*"?([^"\n]*)"?\s*$', block, re.M))
    env = {
        k: ("placeholder" if "var." in v or "local." in v or "data." in v else v)
        for k, v in env.items()
    }
    env.update(
        HOA_TIMEZONE="America/New_York",
        CHAT_DAILY_LIMIT_PER_USER="20",
        CHAT_MONTHLY_BUDGET_USD="5",
        ANTHROPIC_MODEL="claude-haiku-5-5",
        CHAT_PRICE_INPUT_PER_MTOK="",  # empty: the known model's prices
        CHAT_PRICE_OUTPUT_PER_MTOK="",
        AWS_SECRETS_MANAGER_SECRET_ID="",
        # what DEPLOY.md puts in the secret
        SECRET_KEY="x" * 48,
        DATABASE_URL="postgresql+psycopg://u:p@db.example/neondb?sslmode=require",
        DROPBOX_APP_KEY="k",
        DROPBOX_APP_SECRET="s",
        DROPBOX_REFRESH_TOKEN="t",
        ANTHROPIC_API_KEY="sk-placeholder",
    )
    assert env["RETRIEVAL"] == "keyword" and env["APP_ENV"] == "production"
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.is_production and s.retrieval == "keyword" and s.database_null_pool
    assert (s.chat_price_input_per_mtok, s.chat_price_output_per_mtok) == (0.10, 0.50)


def test_lambda_gets_no_empty_settings():
    import re
    from pathlib import Path

    tf = (Path(__file__).parents[1] / "infra/app/main.tf").read_text()
    only_set = r'^\s*env\s*=\s*\{ for k, v in local\.env_all : k => v if v != "" \}$'
    assert re.search(only_set, tf, re.M)
    assert tf.count("variables = local.env\n") == 2  # both functions get the filtered map


# --- email on AWS --------------------------------------------------------------
def test_ses_access_denied_is_permanent():
    from botocore.exceptions import ClientError

    from app.email.providers import EmailSendError, OutgoingEmail, SESProvider

    class Client:
        def send_email(self, **_):
            error = {"Error": {"Code": "AccessDeniedException", "Message": "identity/x"}}
            raise ClientError(error, "SendEmail")

    provider = SESProvider.__new__(SESProvider)
    provider.s, provider.client = get_settings(), Client()
    with pytest.raises(EmailSendError) as exc:
        provider.send(OutgoingEmail(to="a@example.com", subject="s", text="t"))
    assert exc.value.permanent  # shown in last_error now, not retried for hours


def test_lambda_may_send_to_sandbox_recipients():
    from pathlib import Path

    tf = (Path(__file__).parents[1] / "infra/app/main.tf").read_text()
    ses = tf.split('"ses:SendRawEmail"]', 1)[1].split("\n", 2)[1]
    assert ses.strip().endswith(':identity/*"')


def test_migrations_accept_a_url_encoded_password(monkeypatch, capsys):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    url = "postgresql+psycopg://neondb_owner:p%40ss@db.example/neondb?sslmode=require"
    monkeypatch.setattr(get_settings(), "database_url", url)
    root = Path(__file__).parents[1]
    cfg = Config()  # no ini file, so the test's logging setup is left alone
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head", sql=True)  # offline: prints the SQL, connects to nothing
    assert "CREATE TABLE users" in capsys.readouterr().out


def test_migrate_leaves_a_newer_database_alone(monkeypatch, tmp_path, caplog):
    """Rolling back a change that added a migration: the database is ahead of the code."""
    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    from app import lambda_handler

    url = f"sqlite:///{tmp_path / 'rollback.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0099_from_a_reverted_change')"))
    with caplog.at_level("WARNING", logger="app.lambda"):
        result = lambda_handler._migrate()
    assert "0099_from_a_reverted_change" in result and "newer than this code" in result
    assert "newer than this code" in caplog.text
    assert inspect(engine).get_table_names() == ["alembic_version"]  # nothing touched

    # A database this code knows (here, an empty one) is migrated as before.
    calls = []
    monkeypatch.setattr(command, "upgrade", lambda cfg, rev: calls.append(rev))
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite:///{tmp_path / 'new.db'}")
    assert lambda_handler._migrate() == "migrated" and calls == ["head"]
    engine.dispose()


# --- the deploy workflow ---------------------------------------------------------
def _deploy_steps() -> list[dict]:
    from pathlib import Path

    import yaml  # comes with uvicorn[standard]

    wf = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/deploy.yml").read_text())
    return wf["jobs"]["deploy"]["steps"]


def test_deploy_can_be_rerun_for_the_same_commit():
    build = next(s for s in _deploy_steps() if s.get("name") == "Build and push the image")
    script = build["run"]
    assert script.index("aws ecr describe-images") < script.index("docker build")


def test_a_new_secret_value_restarts_the_functions():
    from pathlib import Path

    steps = _deploy_steps()
    names = [s.get("name") for s in steps]
    find = steps[names.index("Find the secret's current version")]
    assert "describe-secret" in find["run"] and "TF_VAR_secret_version" in find["run"]
    assert "get-secret-value" not in find["run"]  # metadata only
    assert names.index("Find the secret's current version") < names.index(
        "Update the tasks function"
    )
    tf = (Path(__file__).parents[1] / "infra/app/main.tf").read_text()
    assert "SECRET_VERSION                = var.secret_version" in tf


def test_migrations_run_before_the_web_function_changes():
    steps = _deploy_steps()
    names = [s.get("name") for s in steps]
    tasks, migrate, full = (
        names.index("Update the tasks function"),
        names.index("Migrate the database"),
        names.index("Apply infrastructure"),
    )
    assert tasks < migrate < full
    targeted = steps[tasks]["run"]
    assert "-target=aws_lambda_function.tasks" in targeted
    assert "-target=aws_iam_role_policy.lambda" in targeted  # lets it read the secret
    assert "-target" not in steps[full]["run"]
    invoke = steps[migrate]
    assert invoke["env"]["AWS_MAX_ATTEMPTS"] == "1"  # no second, overlapping migration
    assert "--cli-read-timeout 900" in invoke["run"]


def test_deploy_warns_when_no_migration_ran():
    from pathlib import Path

    wf = (Path(__file__).parents[1] / ".github/workflows/deploy.yml").read_text()
    assert "newer than this code" in wf and "::warning::" in wf
