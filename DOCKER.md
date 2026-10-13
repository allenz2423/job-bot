# Docker setup

The control UI, optional Gmail worker, and self-hosted ntfy service run in Docker. The application database and private app secrets persist in ignored host directories mounted into the containers. Keep OAuth credentials and other local secrets under the ignored `config/` and `secrets/` directories.

## Start the control UI

Create the persistent directories and local Compose settings:

```sh
mkdir -p config state secrets/app secrets/gmail
chmod 700 config state secrets/app secrets/gmail
cp .env.example .env
```

If the host account does not use UID/GID 1000, edit `.env` and set `JOB_BOT_UID` and `JOB_BOT_GID` to the owner of those directories. Start the UI:

```sh
docker compose up -d --build web
```

Open <http://127.0.0.1:8080>. On first start, retrieve the one-time setup token from the running container and use it to create the initial account:

```sh
docker compose exec web cat /app/state/bootstrap_token
```

The UI container keeps its SQLite state under `state/` and its private encryption material under `secrets/app/`. Back up both directories together. The first owner account sees the existing `state/jobbot.sqlite3` ledger. New member accounts get separate databases under `state/users/`. The candidate profile form uses ordinary fields and “Add another” buttons for repeatable entries. Blank answers stay unknown. The form saves encrypted drafts and requires approval of a specific version before future application use. On the Profile page, users can upload PDF, DOCX, or TXT resumes up to 5 MB as either a primary resume or a targeted resume with a role or region. Each file can be labeled, downloaded, retagged, approved, or have approval revoked. Reclassifying a file clears its approval; choosing a new primary demotes the previous primary to unassigned. Existing uploads start unassigned until the user classifies them. Pasted text remains available for the primary resume, and older text-only role-specific entries remain visible read-only. The app stores original files and extracted text encrypted per user. It extracts selectable PDF text using `pdftotext`, reads DOCX and TXT locally, and offers only explicit header contact details, a clearly labeled summary, and listed skills as draft suggestions. Scanned PDFs without selectable text need manual paste. Uploading a primary resume does not replace existing saved primary text unless the user selects that option. Approved primary originals can be attached to supported application forms. The **Apply queue** page accepts a full job URL you want the applier to inspect. Each account has a private queue, review decisions, and recovery history.

## Local model setup

Discovery and application use separate local Gemma 4 models through an OpenAI-compatible endpoint. The defaults in `.env.example` use `gemma4:e2b` for public search-result classification and `gemma4:e4b` for interpreting application forms. Both use `LOCAL_LLM_BASE_URL`, which defaults to `http://host.docker.internal:11434/v1` for a local Ollama endpoint. Compose maps `host.docker.internal` for the discovery and applier containers. If your local endpoint differs, edit `.env`; do not put provider credentials or other secrets in model settings or prompts. The optional legacy `job-bot` Gmail worker does not use these models.

The discovery model receives public search-result titles and snippets for classification. The applier model receives only the form labels/instructions and approved candidate facts needed for the current application. It can interpret how a form asks for supplied facts, but it must not invent qualifications, work history, eligibility, or screening answers. Passwords, OAuth tokens, site credentials, and other secrets are never sent to either model. Unknown, not-applicable, and withheld values remain distinct; a required unknown or unapproved answer pauses for review. Signatures, legal attestations, high-risk identity or onboarding requests, CAPTCHAs, verification challenges, and site blocks require a human handoff.

The Job preferences section treats its entries as soft hints for discovery and ranking, and includes an Other preferences list for factors not covered by the named fields. Only entries in Hard dealbreakers are intended to block a future application. The discovery ranker may flag a possible conflict for review, but never discards a listing based on a preference or model result. Saved **Job searches** in Settings are explicit keywords that direct the scraper to search for roles.

## Keyword job scraper

In **Settings → Job searches**, add one phrase for each field or role to search, such as `nursing`, `HPC engineering intern`, `SWE intern`, or `mechanical engineering full time entry level`. Location is optional and each phrase can be disabled or removed. These phrases are encrypted per user. The broad-search connector uses the official [Adzuna job search API](https://developer.adzuna.com/docs/search), which needs an app ID and key from [Adzuna Developer](https://developer.adzuna.com/). Each user enters their own credentials in **Settings → Adzuna search access**; the fields are write-only in the UI and encrypted in that user's tenant database. No Adzuna credential is sent to the local model, queue, logs, or notifications.

Check [Adzuna's API terms](https://developer.adzuna.com/docs/terms_of_service) before enabling it. Personal research is a listed use, while ongoing commercial use may require a license after a 14-day evaluation. The UI requires an access-rights confirmation. Its default quota is 250 requests per day; saved keyword phrases run no more than once every six hours, while each API key is capped at 200 requests per rolling day and 20 per minute. Each query returns one page of at most 50 listings. The Adzuna API returns a description snippet, so the full job page will still need inspection by the future application worker. It preserves the exact returned job URL and labels displayed listings with Adzuna attribution. The connector currently supports US, UK, Canada, and Australia; its coverage depends on Adzuna's index.

Before the first start, create SearXNG's ignored local secret file with `python3 scripts/init_searxng.py`. Then start the web, discovery, and SearXNG services with `docker compose up -d --build`; they restart automatically if they exit or Docker restarts. Discovery waits for SearXNG's local health check before its first scan. SearXNG is available only inside the Docker network. Approve the current profile and unpause discovery in Settings, then add search phrases. The worker searches them without requiring source URLs or an external search API key. In **Settings → Scrape jobs**, every entry has its own target for new, unique jobs to queue (1–200) and wakeup interval (1 minute to 24 hours, default: 6 hours). Search jobs keep scanning additional SearXNG pages while the target is unfilled, skipping duplicate URLs; each run logs its target and actual new queue count. A source may finish short of its target when it has too few valid listings or reaches a provider limit. The discovery service checks jobs every minute by default. Saved keyword phrases remain on a separate six-hour cadence. Saved searches can also use optional Adzuna credentials.

## Optional RSS/Atom feeds

The discovery worker searches the public web through the private SearXNG service, which aggregates the engines enabled in its configuration, and also reads HTTPS RSS/Atom feeds or scrapes explicitly configured public job pages. SearXNG's JSON response format is enabled in `config/searxng/settings.yml`; its HTTP endpoint is not published on the host. For configured page scraping, it checks robots.txt, parses embedded schema.org JobPosting records, and follows at most 30 same-host links with job-related paths per source scan. For a collection page found by search, it follows up to two same-host link levels, fetches at most ten pages per root, and stops after 30 seconds; no more than two collection roots are crawled per search. Only structured `JobPosting` records with individual job URLs are queued. It identifies itself with a descriptive user agent, limits page size to 2 MB, and rejects non-public hosts and cross-host redirects. Check each site's terms before enabling a source. It does not log in, evade site blocks, solve CAPTCHAs, or use browser fingerprint tricks.

In the control UI, open **Settings → Scrape jobs** to add target job titles and optionally a permitted employer careers URL. Every source row has its own **Target new jobs per cycle** (1–200) and **Run every** interval (1 minute, 5 minutes, 10 minutes, 15 minutes, 30 minutes, 1 hour, 2 hours, 4 hours, 6 hours, 12 hours, or 24 hours); edit a row's values and choose **Save**. The worker counts new unique queue items, not duplicate URLs, toward the target and keeps paging through up to 50 SearXNG pages per title as needed. Each search page counts against the SearXNG cap of 500 requests per rolling day and 20 per minute. If the search has fewer valid individual jobs or reaches the provider cap, the run logs how far it got. Select **Run now** to request an immediate scan of that source. The request is stored with that account's source and discovery picks it up on its next pass; request caps, profile approval, and the discovery pause still apply. The posting date range applies when a listing supplies a date; undated results may still enter the queue. A local model can interpret equivalent titles (for example, "HPC Intern" and "High Performance Computing Intern"); a simpler fallback matcher runs for title matching without a model. Without a source URL, the worker searches SearXNG. The local `gemma4:e2b` model, configured by `LOCAL_LLM_MODEL`, uses a strict system prompt to classify each result as an individual job, a collection page, other, or uncertain. The result's site/path can help reject collection pages, but only its title/snippet may supply a role or employer; the code checks that extracted phrases appear in that result. Collection pages may seed the bounded public crawl described above, but the collection URL itself is never queued. If the model is unavailable or returns malformed output, the search retries after 15 minutes under the provider cap, without adding raw results. Unknown employers remain blank, and search phrases are never used as companies. The source is labeled SearXNG, and each individual job's exact full URL is the identifier for deduplication and recovery. Saved phrase searches have a separate six-hour cadence. Confirm that an employer URL permits automated reading. For configured pages, the worker checks robots.txt, reads structured postings, and follows up to 30 same-host job links on each source's selected schedule. Sources are private to your account. Each account must have an approved current profile and discovery must be unpaused in **Settings**. The worker checks for new settings every minute by default; set `DISCOVERY_INTERVAL_SECONDS` in `.env` to a value from 60 to 86400 seconds. Start it with:

```sh
docker compose up -d --build discovery
```

To scan once instead of starting the long-running service:

```sh
docker compose run --rm discovery python3 scripts/discovery_worker.py --once
```

Every new listing is scored immediately by the local `embedding-ranker` service. It loads EmbeddingGemma 2 with its vision and audio encoders disabled, verifies that both the loaded model and exported checkpoint contain 250–300 million text parameters, saves the text-only model under ignored `models/embeddinggemma/`, and deletes the full Hugging Face download cache after verification. The model card describes this text-only configuration as 270M parameters; that is a parameter count, not a 270 MB file size. The service uses 256-dimensional normalized embeddings. Scores compare the job with saved search phrases and approved soft preferences and remain advisory. The first startup downloads the upstream checkpoint and exports the reduced local copy, so leave enough disk space for the temporary download and export. Later starts use the saved copy. If there are no search phrases or ranking preferences, or the ranker becomes unavailable, the listing still enters the queue as **Not ranked** with a reason. A possible hard-dealbreaker match from the optional local LLM is only a review hint; no ranking score silently filters a job.

The discovery worker uses the existing per-user exact-URL duplicate checks and durable queue for both keyword searches and feeds. Feed validators are tracked per user and advance only after that user's queue pass completes, so a newly enabled account gets its own first read and interrupted passes can be retried. The Jobs page links to each item's saved full URLs and recent state changes. Discovery skips inactive accounts, accounts requiring a password change, paused discovery, and profiles that are not approved at the current version.

## Apply queue and application worker

The **Apply queue** page accepts a job's exact full URL, with optional company and role details. You can also add a listing from the **Global pool**. The `applier` Compose service checks the queue every ten seconds while idle and recovers expired leases from durable history. It starts with `docker compose up -d --build`, or by itself with `docker compose up -d --build applier`.

For review-only preparation, uncheck **Pause applications**, choose **Prepare for review**, approve the current profile, and approve a primary resume with readable extracted text. For automatic submission, select **Submit eligible**. The worker attempts simple applications for queued job sites using approved profile facts and the approved primary resume. The daily limit defaults to five; use `-1` for unlimited or `1`–`100` for a finite cap. **Discovery only**, application pause, and unapproved profile/resume state keep submission disabled.

For each eligible item, the worker claims a durable lease, checks the public HTTPS URL and robots.txt, then opens the page in headed Chromium through loopback-only CDP. Chromium runs in an Xvfb display inside the container; no CDP port is published. The `applier` service alone uses `docker/applier-seccomp.json`, based on [Moby's Docker 29.8.2 default profile](https://github.com/moby/moby/blob/docker-v29.8.2/vendor/github.com/moby/profiles/seccomp/default.json), to allow Chromium's namespace sandbox. Review that profile when upgrading Docker. The local `gemma4:e4b` applier model interprets bounded form labels and controls and selects exact approved reusable profile facts. A model-written narrative sends the job to review until the user supplies an approved answer. If the listing has one explicit same-host Apply link, the worker may inspect that linked page after a separate public URL and robots.txt check; the original full job URL remains the ledger key. The worker verifies the approved primary resume checksum and readable text before planning. A single clearly labeled resume upload can receive that file; the temporary copy lives in the applier's 16 MB tmpfs and is removed after the attempt or when the container restarts. Unknown or unapproved required answers, legal attestations, signatures, high-risk disclosures, verification challenges, site blocks, ambiguous forms, and outcomes go to **Needs review**. The browser does not bypass CAPTCHAs or site controls.

Before clicking the submit button, the worker rechecks the account mode, pause setting, current profile and resume approvals, and daily limit. It records the exact URL as in progress before the click and marks the queue complete only after a recognized employer confirmation appears. If the result is uncertain or the container stops during submission, the item stays in review for a site and history check before retrying. On CAPTCHA or sign-in verification, the applier keeps that user's exact browser session open. The owning account sees **Open browser view** at the top of **Apply queue** and on its authenticated job history page; the VNC password appears only on that history page. The viewer uses the localhost-only VNC service (`127.0.0.1:5900`, noVNC at `127.0.0.1:6080`). Only one human handoff is active at a time, so the viewer displays only that owner's browser session. Solve verification manually, then approve the job for a retry, or inspect the application outcome before choosing **Submission confirmed on site** or **No submission on site**. A separate button closes the VNC browser while leaving the job in review. The `discovery` container has no VNC processes or published VNC ports. In **Prepare for review**, the worker never submits. The applier sends the local model only the visible form labels/instructions and approved facts needed for the current answer, never passwords, OAuth tokens, site credentials, or other secrets. It uses fresh task context for each application and does not persist model prompts or responses in the application history.

### Shared job pool and URL recovery

Public listings found through SearXNG and permitted public pages or feeds are also stored by exact full URL in `state/global_jobs.sqlite3`, which is already inside the persistent Docker state mount. Adzuna results stay in the discovering user's private queue because its API access confirmation does not establish redistribution rights. The **Global pool** page shows recent shared listings and lets an account add one to its own private queue. Each account retains its own duplicate checks, application ledger, leases, and history. A manually entered URL stays private unless **Share this public listing in the global pool** is selected; existing private queue items can be shared from their history page. URLs with obvious private query parameters are rejected from the shared pool. Existing jobs are not automatically published during the migration.

The queue adapter exposes `report_invalid_url` for a worker holding a lease. It requests a durable rescrape of that pool job, pauses only that user's queue item, and returns an instruction to discard task context before claiming another job. The discovery worker checks up to five such requests per cycle. It fetches only the exact HTTPS job URL when a currently enabled, user-confirmed job-page source covers the same host; robots.txt must permit that URL. A structured JobPosting at that exact URL verifies the job and lets paused private items resume. A changed URL, missing posting, block, or missing source permission leaves the item paused for review. The job history page offers **Retry URL check** after a permitted source is configured.

### Optional network access

The default Compose binding publishes the UI only on `127.0.0.1`. To publish it on all host interfaces, edit `.env` and set both values below exactly:

```dotenv
WEB_HOST_BIND=0.0.0.0
WEB_REMOTE_BIND_ACK="I understand this exposes the job bot to my network"
```

The UI refuses to start with a `0.0.0.0` binding unless the acknowledgment matches exactly. Other devices on reachable networks can connect; keep authentication enabled, and use a trusted LAN or VPN, firewall rules, and HTTPS for non-local access. The service still listens on port 8080 inside Docker; `WEB_HOST_PORT` changes the host-side port.

## Local ntfy service

Create the per-install topic and matching access rule once:

```sh
docker compose --profile tools run --rm --no-deps tools scripts/setup_ntfy_config.py
```

Start ntfy:

```sh
docker compose up -d ntfy
```

The server is bound to `127.0.0.1:8090`, and only the generated high-entropy topic is allowed. Its URL is `http://127.0.0.1:8090/<topic>`; the topic is in the ignored `config/ntfy.env`. Persistent cache and auth data live under ignored `data/ntfy/`.

This is local-only. For phone access away from the Docker host, put ntfy behind an HTTPS reverse proxy and change its `base-url`; do not publish the current HTTP port directly to the internet.

## Optional Gmail worker

The `job-bot` image includes Python, `python3-cryptography`, `poppler-utils`, and Bitwarden CLI 2026.9.1. The Gmail worker stays in a Compose profile so it does not start accidentally. Gmail sync is disabled until `GMAIL_ENABLED=true` is set in `config/gmail.env`. After Gmail OAuth is configured, start the worker with:

```sh
docker compose --profile worker up -d --build job-bot
```

The worker reads the host-mounted OAuth token and writes SQLite state to `state/`. Vaultwarden API credentials are loaded from the ignored `config/vaultwarden.env`; the CLI is configured for `BW_SERVER_URL` at startup. The worker unlocks the CLI at startup from `secrets/bitwarden_master_password`, mounted read-only as a Docker Compose secret. Keep that file owner-only (`chmod 600`); it is ignored by Git. Do not put the master password in an env file. Anyone who can read the host file or control the worker container can access the vault while the worker is running.

The `job-bot` worker command performs Gmail metadata sync using the original single-user configuration. It does not access member accounts or run browser application workflows. Discovery and application preparation are separate always-on `discovery` and `applier` Compose services. Do not treat the current Gmail, Sheets, or Vaultwarden files as per-member credentials.

## Application tracker

Run tracker commands inside the short-lived tools container after its local configuration files are set up:

```sh
docker compose --profile tools run --rm --no-deps tools scripts/sheets_tracker.py create
docker compose --profile tools run --rm --no-deps tools scripts/sheets_tracker.py sync
```

The generated workbook URL and ID are saved in ignored `config/sheets.env`. It has a dashboard, application list, review queue, filters, status colors, and editable Priority, Your Notes, and Review Decision columns. The exact full job URL is shown as the row identifier. Sync recognizes older rows whose first column contained an internal application key, matches them using that key or their saved exact URL, and changes the first column to the URL while preserving the three user-owned columns. Sync imports those three columns into SQLite, then updates bot-owned columns without overwriting them. The `sync_application_tracker` tool in `scripts/application_tools.py` performs the same pull-and-push for a local LLM tool adapter.

The ledger's existing local tool interface operates on the original owner database and can be called from the container with JSON on stdin. For example:

```sh
printf '%s' '{"job_url":"https://jobs.example/123","company":"Example","role":"Analyst"}' | docker compose --profile tools run --rm -T --no-deps tools scripts/application_tools.py upsert_application
printf '%s' '{}' | docker compose --profile tools run --rm -T --no-deps tools scripts/application_tools.py recover_in_progress_applications
```

The worker records the exact URL before starting an application and only records `applied` after a confirmed submission. The ledger can return in-progress URLs for crash recovery. These tools provide durable tracking; the browser/application decision loop still needs to be connected before the worker can submit applications.

## Bitwarden master password file

Create or update the local secret without putting it in shell history:

```sh
mkdir -p secrets
chmod 700 secrets
read -rsp 'Bitwarden master password: ' JOBBOT_BW_MASTER; printf '\n'
printf '%s\n' "$JOBBOT_BW_MASTER" > secrets/bitwarden_master_password
unset JOBBOT_BW_MASTER
chmod 600 secrets/bitwarden_master_password
```
