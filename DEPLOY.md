# Deploying to AWS

This sets the portal up on AWS for a few dozen people at close to no cost. Once it's set up, every merge to `main` deploys automatically.

## What runs where

| Piece | Service | Cost for ~25 people |
|---|---|---|
| Web app | AWS Lambda (container image) behind an API Gateway HTTP API | $0 (Lambda's always-free tier: 1M requests and 400,000 GB-seconds a month) |
| Background work (email, Dropbox checks, migrations) | A second Lambda, run hourly by EventBridge and on demand by the web app | $0 |
| Database | [Neon](https://neon.com) Postgres, free plan | $0 |
| Documents | The board's Dropbox folder (read-only) | $0 |
| Secrets | One AWS Secrets Manager secret | $0.40 a month |
| Email | The HOA's free Gmail account, over SMTP | $0 (Gmail allows about 500 recipients a day) |
| Container images | ECR (keeps the last 5) | a few cents |
| Logs | CloudWatch, kept 30 days | about $0 |
| Chatbot | Anthropic API, prepaid credits | capped by the portal's monthly budget (default $5) |

**Total: about $0.50 a month for AWS, plus whatever the chatbot uses, which can't pass the budget you set.** API Gateway is free for the first 12 months and about a cent a month after that at this size. No domain is needed.

There's no VPC and no NAT gateway (a NAT gateway alone would be about $32 a month). The functions reach Neon, Dropbox, Anthropic and Gmail (`smtp.gmail.com`, port 587) over the internet with TLS.

### Email without a domain

The portal sends its email (invitations, password resets, questions to management, announcements) from a free Gmail account the HOA creates for it, such as `yourhoa@gmail.com`, using an app password. People see that address as the sender, and replies go to it. Gmail lets an account send to about 500 recipients a day. That's plenty here: an announcement to every household is one email per recipient, so even 150 homes leaves room for the day's other email. Anything Gmail refuses is retried hourly, up to five times. If the HOA ever gets its own domain, it can switch to Amazon SES ([step 6](#6-optional-send-through-amazon-ses-with-your-own-domain)).

### Why Neon and not Supabase

Both have free Postgres with pgvector. Neon fits Lambda better:

- **Neon free plan:** **1 GB storage per project**, 100 compute-hours a month, 5 GB of data transfer a month, pgvector and connection pooling included, no card needed. The database sleeps after 5 minutes idle and wakes in under a second on the next request. It is never paused for inactivity.
- **Supabase free plan:** 500 MB, and the project is **paused after a week without activity** and must be restored by hand, which is a real risk for a portal people use a few times a month. No backups on the free plan.

What Neon's limits mean here: the portal's data (accounts, bookings and the text of a few hundred documents) is well under 1 GB. Compute is billed only while the database is awake. The hourly background check wakes it 24 times a day for about 5 minutes each, which is about 15 compute-hours a month at the smallest size, leaving plenty for real use. If a limit were ever hit, Neon suspends the database until the next month (it doesn't charge you), so watch the usage page in Neon's console for the first month. The paid plan, if ever needed, is pay-as-you-go at roughly $0.11 per compute-hour.

### The chatbot and its limits

The chatbot uses Claude through the Anthropic API with a **separate API key on prepaid credits**, not your personal Claude account. Usage is limited three ways:

1. **Per person:** 20 questions a day (`CHAT_DAILY_LIMIT_PER_USER`). After that the assistant points them to management until the next day. Deleting a conversation doesn't reset the count.
2. **Portal-wide:** a monthly budget of $5 (`CHAT_MONTHLY_BUDGET_USD`). The portal records the tokens of every answer and pauses the assistant for everyone when the month's estimated spend reaches the budget. People see a friendly message with a link to send their question to management instead. It comes back on the 1st. Admins see the month's spend under the chat box.
3. **The hard backstop, in the Anthropic Console:** prepaid credits with auto-reload off, and a spend limit on the workspace (step 3). Even if the portal's own count were wrong, Anthropic stops at that limit.

Each answer uses Claude Haiku 5.5 (`claude-haiku-5-5`, $0.10 per million input tokens and $0.50 per million output tokens), with extended thinking off and at most 1,000 output tokens. A typical question sends about 6,000 tokens of handbook text, so it costs about a tenth of a cent. $5 covers roughly 5,000 questions a month. To change any of these, edit the defaults in `infra/app/variables.tf` (or set `TF_VAR_...` in the deploy workflow) and merge. The portal knows Claude Haiku 5.5, Sonnet 5.5 ($2 / $10 per million tokens) and Opus 5.5 ($4 / $20): switching `anthropic_model` to one of them also switches the prices the budget counts with and the way thinking is turned off, which differs between them. For any other model, also set `chat_price_input_per_mtok` and `chat_price_output_per_mtok`, or the functions refuse to start rather than count the wrong spend. Opus 5.5 can't switch thinking off, so its answers also pay for thinking tokens: the portal runs it at low effort with room for 4,000 output tokens, which makes an answer cost a few cents rather than a tenth of one.

**Search** uses Postgres full-text search to find the handbook passages to send with each question, so there's no second paid account for embeddings. It matches words and their simple variants ("fence", "fences", "fencing"), not synonyms. If answers miss too often, switch to Voyage embeddings (`RETRIEVAL=vector`, `EMBEDDING_PROVIDER=voyage`, a `VOYAGE_API_KEY`), which has a free allowance and is described in the README.

## What you need first

- An AWS account you can sign in to as an administrator, with the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) set up (`aws sts get-caller-identity` should work).
- [Terraform](https://developer.hashicorp.com/terraform/install) 1.10 or newer.
- The [GitHub CLI](https://cli.github.com) signed in with access to this repository (or you can set the variables on github.com).
- A free Gmail account for the HOA (step 4 says how to set it up). No domain is needed.
- Python 3.12 and this repository cloned, for the one-time commands in steps 4 and 8.
- The Dropbox folder and Dropbox app from the README's **Documents (Dropbox)** section, and the refresh token from `python -m app.cli dropbox-auth`.

Pick one AWS region and use it everywhere. The examples use `us-east-1`.

## Steps

### 1. Set up AWS (once)

This creates the pieces the automatic deploys need: a bucket for Terraform's state, the container registry, the empty secret, and a role that GitHub Actions can use without any stored AWS keys. The role can only be used by workflows running on this repository's `main` branch.

```
cd infra/bootstrap
terraform init
terraform apply -var github_repo=OWNER/REPO -var region=us-east-1
```

Leave out `email_domain`: it's only for sending through SES with your own domain (step 6). If your AWS account already has GitHub's OIDC provider (from another project), add `-var create_github_oidc_provider=false`.

Keep the `terraform.tfstate` file this creates somewhere safe (it isn't secret, but you'll need it to change or remove these pieces later). It's ignored by git.

### 2. Create the database on Neon

1. Sign up at [neon.com](https://neon.com) and create a project. Choose the AWS region closest to the one you picked (for `us-east-1`, AWS US East N. Virginia) and Postgres 17.
2. In the project's compute settings, set autoscaling to **0.25–0.5 CU**. That's plenty for this portal and makes the free compute-hours last.
3. Under **Connect**, turn **Connection pooling off** and copy the connection string. The portal opens a short connection for each request and closes it, so the database can go back to sleep between visits.
4. Change its start from `postgresql://` to `postgresql+psycopg://` and make sure it ends with `?sslmode=require`. It should look like:
   `postgresql+psycopg://neondb_owner:PASSWORD@ep-example-123456.us-east-1.aws.neon.tech/neondb?sslmode=require`

### 3. Set up the Anthropic API key with a hard limit

1. Sign in at [console.anthropic.com](https://console.anthropic.com). This is separate from a claude.ai Pro subscription and doesn't use it.
2. Create a workspace for the portal (Settings → Workspaces), and set its **spend limit** to the most you're willing to spend in a month, for example $10.
3. Under Billing, buy a small amount of prepaid credit (for example $10) and leave **auto-reload off**. When the credit runs out, the API simply stops answering and the portal tells people to ask management.
4. Create an API key in that workspace. You'll paste it into the secret in the next step.

### 4. Fill in the secret

First, the Gmail account the portal sends from:

1. Create a new Gmail account for the HOA at [accounts.google.com](https://accounts.google.com/signup), for example `yourhoa@gmail.com`. Use it only for the portal, and give a board member the recovery phone.
2. Turn on **2-Step Verification** for it (Google Account → Security). App passwords need it.
3. Create an **app password** (Google Account → Security → 2-Step Verification → App passwords, or search the account settings for "App passwords"). Name it "HOA portal". Google shows a 16-letter password once; copy it. It only lets the portal send and read mail, can't change the account, and you can revoke it there at any time.

Then make a random session key:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put everything in a file outside the repository, for example `~/hoa-secret.json`:

```json
{
  "SECRET_KEY": "the random value from above",
  "DATABASE_URL": "postgresql+psycopg://…?sslmode=require",
  "DROPBOX_APP_KEY": "…",
  "DROPBOX_APP_SECRET": "…",
  "DROPBOX_REFRESH_TOKEN": "…",
  "ANTHROPIC_API_KEY": "…",
  "SMTP_USERNAME": "yourhoa@gmail.com",
  "SMTP_PASSWORD": "the 16-letter app password, without spaces"
}
```

Then store it and delete the file:

```
aws secretsmanager put-secret-value --region us-east-1 \
  --secret-id hoa-portal/production --secret-string file://$HOME/hoa-secret.json
rm ~/hoa-secret.json
```

To change a value later, run the same command with the whole JSON again, then run the **Deploy** workflow from the Actions tab (or wait for the next merge). The deploy passes the secret's current version to the functions, so a new value restarts them and they read it; re-running for a commit that's already deployed reuses its image.

### 5. Tell GitHub where to deploy

`terraform output github_variables` (in `infra/bootstrap`) prints three values. Set them, plus the portal's settings, as repository **variables** (not secrets; none of these is sensitive):

```
gh variable set AWS_REGION --body us-east-1
gh variable set AWS_DEPLOY_ROLE_ARN --body arn:aws:iam::123456789012:role/github-actions-hoa-portal
gh variable set TF_STATE_BUCKET --body hoa-portal-tfstate-123456789012
gh variable set HOA_NAME --body "Your HOA name"
gh variable set EMAIL_FROM --body "Your HOA <yourhoa@gmail.com>"
gh variable set MANAGEMENT_NOTIFY_EMAILS --body "office@yourmanagementcompany.com"
```

`EMAIL_FROM` must be the Gmail account's own address, or Gmail replaces it with that address. Deploys are skipped until `AWS_DEPLOY_ROLE_ARN` is set, so nothing happens before this step.

### 6. Optional: send through Amazon SES with your own domain

Skip this unless the HOA has a domain and outgrows Gmail's daily limit. SES costs $0.10 per 1,000 emails.

1. Run step 1's `terraform apply` again with `-var email_domain=yourhoa.org` added. Then `terraform output ses_dkim_records` (in `infra/bootstrap`) prints three CNAME records. Add them at your DNS host. SES shows the domain as verified within a few hours.
2. Add an SPF record if you don't have one (`v=spf1 include:amazonses.com ~all` on the domain) and a DMARC record (`_dmarc.yourhoa.org`, `v=DMARC1; p=none;`).
3. New SES accounts are in a **sandbox** that only sends to addresses you've verified. In the SES console, choose **Request production access**, and describe it as transactional email for an HOA portal (invitations, password resets, homeowners' questions to management and board announcements to members who signed up). Approval usually takes a day. Until then, you can verify your own address in SES to test.
4. Switch the portal over and redeploy:
   ```
   gh variable set EMAIL_PROVIDER --body ses
   gh variable set EMAIL_DOMAIN --body yourhoa.org
   gh variable set EMAIL_FROM --body "Your HOA <no-reply@yourhoa.org>"
   ```
   The Gmail login can stay in the secret; it's not used for sending with SES.

### 7. Deploy

Merge the pull request into `main`, or if it's already merged, run the **Deploy** workflow from the Actions tab. It runs the tests, builds the image, updates the tasks function, runs the database migrations with it, applies the rest of `infra/app` and checks that the site answers. If the first run fails (say the secret wasn't filled in yet), fix the cause and use **Re-run jobs**: the image built the first time is reused. On the very first deploy the migration can also fail with an access-denied error on the secret, because AWS takes a few seconds to apply the functions' new permissions; re-run it. The workflow summary shows the portal's address, which looks like `https://abc123.execute-api.us-east-1.amazonaws.com`.

### 8. Create the first administrator

From your clone of the repository, with the Neon connection string from step 2:

```
pip install -e .
DATABASE_URL='postgresql+psycopg://…?sslmode=require' python -m app.cli create-admin you@example.com
```

It asks for a password. Sign in at the portal's address and invite everyone else from the People page. The first Dropbox import runs within the hour, or press **Check Dropbox now** on the Documents page.

### 9. Optional: your own web address

To serve the portal at, say, `portal.yourhoa.org`:

1. In AWS Certificate Manager (same region), request a certificate for that name and add the CNAME it shows to your DNS.
2. Once it's issued, set two more variables:
   ```
   gh variable set CUSTOM_DOMAIN --body portal.yourhoa.org
   gh variable set CUSTOM_DOMAIN_CERTIFICATE_ARN --body arn:aws:acm:us-east-1:…
   ```
3. Re-run the Deploy workflow, then add a CNAME from `portal.yourhoa.org` to the `custom_domain_target` value in its output. Email links switch to the new address automatically.

### 10. Optional: an address to cc, so the weekly review reads email chains

Board and management members can add the HOA's Gmail address as a recipient on any email chain the weekly review should know about, such as a contractor's quote or a renewal notice. The portal reads that inbox once a day and just before each weekly review, and shows what it kept on the **Inbox** page.

It reads the inbox without changing it: nothing is marked read, moved or deleted there, and it never sends from it for this. It keeps a message only when Gmail confirms it really came from an active board, admin or management member of the portal. Vendors' replies and anything else are skipped; a member's reply that quotes them is kept. It keeps the text only, no attachments, and deletes it after 90 days. Management sees only chains a management user or the management company's inbox was on.

To turn it on, use the same Gmail account the portal sends from:

1. In that Gmail account, open Settings → **See all settings** → **Forwarding and POP/IMAP**. If it offers **Enable IMAP**, choose it and save; newer accounts have IMAP on already.
2. Set the address, then re-run the **Deploy** workflow from the Actions tab:
   ```
   gh variable set INBOX_ADDRESS --body yourhoa@gmail.com
   ```
   It uses the app password already in the secret (`SMTP_PASSWORD`). To use a separate Gmail account instead, give it its own app password as `INBOX_APP_PASSWORD` in the secret (step 4).

Then tell the board and management the address. Mail that reaches the inbox before it's turned on is read too, back to 10 days.

## Day to day

- **Deploys** happen on every merge to `main`. A failed test stops the deploy before anything changes. After the image is built, the tasks function moves to the new code and runs the migrations; only then does the web function move. So a failed migration leaves the web pages on the previous code and the workflow fails; fix it and merge again. Until then the tasks function runs the new code against the old schema, and on Lambda it does all the email and Dropbox work: if the release touches a table the migration was meant to change, password resets, invitations, notifications and syncs may fail until the fix deploys.
- **Migrations must work with the previous release**, because the web function runs the old code against the new schema for the minute between the migration and its own update. Add columns as nullable or with a default, and drop or rename one only in a later release, once no deployed code uses it.
- **Logs** are in CloudWatch under `/aws/lambda/hoa-portal-web` and `/aws/lambda/hoa-portal-tasks`.
- **Background work.** Email goes out right after the action that caused it. Every hour the tasks function retries any email that failed and checks Dropbox. So a file moved in Dropbox shows its new place (and the chatbot its new text) within the hour, or straight away after **Check Dropbox now**. Change the schedule with `worker_schedule` in `infra/app/variables.tf`.
- **Large downloads.** Lambda can return at most 6 MB, so files over 4 MB are handed to Dropbox as a temporary download link (valid for four hours) after the portal checks the person may see them, in the folder the file is in now.
- **Rolling back.** Revert the change on `main`; the merge deploys the previous version. A rollback keeps the database as it is: if the reverted change added a migration, the older code finds the database at a revision it doesn't know, leaves the schema alone and says so in the deploy log. That's safe because of the rule above, which is why it matters: a migration the previous release can't run against has no way back. To bring a reverted change back, revert the revert, or build its new migration on top of the reverted one. A new migration built on the older revision would be skipped as "older than the database" (the deploy shows a warning), and the new code would run against the wrong schema.
- **If creating a function fails on the image** ("Lambda does not have permission to access the ECR image") on the first deploy, Lambda wanted to add its own statement to the repository policy. Give the deploy role `ecr:SetRepositoryPolicy` on that one repository (add it to the `EcrPush` statement in `infra/bootstrap/main.tf` and apply bootstrap again), then re-run the deploy.
- **Use a dedicated AWS account if you can.** The deploy role is limited to this app's functions, roles, logs, schedule and secret, but its API Gateway permissions cover every HTTP API and custom domain in the region. In an account shared with other projects, it could change theirs.
- **Removing everything.** `terraform destroy` in `infra/app` (with the same `-backend-config` as the workflow), then in `infra/bootstrap`. Delete the Neon project and the Anthropic key separately.
