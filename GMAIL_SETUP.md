# Gmail setup for the local job bot

This repository includes the local OAuth launcher and a metadata-only Gmail sync command. The sync uses the Gmail REST API directly, so no Google API client package is required.

## Integration choice

Use the normal Gmail REST API from a local Gmail adapter, then expose only narrow bot tools such as `search_employer_updates` and `get_message_for_handoff`. This fits the scheduled 24/7 polling flow and keeps the OAuth token handling inside this app. Google's official [remote Gmail MCP server](https://developers.google.com/workspace/gmail/api/reference/mcp) is currently in Developer Preview; it is an option if the app later standardizes on remote MCP, but this setup does not require it or a Gmail MCP API enablement.

## What you need

- A Google account with Gmail.
- A Google Cloud project with the Gmail API enabled.
- A Desktop OAuth client ID and client secret.
- One-time consent from the Google account the bot should read.
- A Gmail label or search query that identifies the employer messages to scan.

No Google API key is needed. Gmail and Sheets are authorized with OAuth. The bot will request `gmail.readonly` for mail and `drive.file` for the spreadsheet it creates or selects. Gmail's scope permits reading messages and settings and is a Google restricted scope; it is not a per-sender or per-label permission. The Gmail query narrows what the app fetches, while the OAuth scope itself can read broadly. Google recommends `drive.file` for per-file spreadsheet access. See [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) and [Sheets scopes](https://developers.google.com/workspace/sheets/api/scopes).

## Google Cloud setup

1. [Create a Google Cloud project](https://console.cloud.google.com/projectcreate).
2. [Enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com) for that project.
3. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) and configure the app:
   - Give it a recognizable name, such as `Job Bot Local`, and provide contact emails.
   - For a regular Gmail account, set the audience to **External** and add your Google account as a test user while setting it up. Use **Internal** only when the account is in the same Google Workspace organization as the Cloud project.
   - Add these scopes: `https://www.googleapis.com/auth/gmail.readonly` and `https://www.googleapis.com/auth/drive.file`.
4. Under **Clients**, create an OAuth client with application type **Desktop app**. Google's [Gmail desktop quickstart](https://developers.google.com/workspace/gmail/api/quickstart) documents this flow.
5. Copy `config/gmail.env.example` to `config/gmail.env`, then set `GMAIL_OAUTH_CLIENT_ID` and `GMAIL_OAUTH_CLIENT_SECRET` from the Desktop client. Do not commit `config/gmail.env`.
6. Run `python3 scripts/authorize_google.py` on the machine that will run the bot. It starts a loopback callback and prints a one-time Google authorization link. Open that link in a browser on the same machine, sign in to the intended account, and approve the Gmail and file-level Sheets access. The script writes the refresh token to `secrets/gmail/token.json`; do not share or commit that file.

## Gmail query and verification codes

Create a Gmail label named `job-bot-updates` and a Gmail filter for relevant employer messages, then keep the example `GMAIL_SEARCH_QUERY` or narrow it to the senders you expect. Gmail search terms are a fetch filter, not an OAuth security boundary.

After you have created the label/filter, set `GMAIL_ENABLED=true` in `config/gmail.env` and run:

```sh
python3 scripts/sync_gmail.py
```

For repeated polling in the foreground, run `python3 scripts/sync_gmail.py --watch`. It uses `GMAIL_POLL_INTERVAL_SECONDS` (300 seconds by default) and can later be managed by a service supervisor.

The command searches only `GMAIL_SEARCH_QUERY`, requests message headers (not bodies or snippets), and upserts message IDs, thread IDs, sender, date, labels, and a sanitized subject into `state/jobbot.sqlite3`. It reports counts only. Verification-related subjects are redacted and standalone 4–8 digit sequences are removed before storage. The sync checks up to `GMAIL_SYNC_MAX_MESSAGES` per run (default 100); repeated runs safely update existing records. OAuth refreshes happen automatically and the local token file is replaced with owner-only permissions.

The bot may detect a relevant verification code and present it for a human handoff. It must not log or persist the code, and `GMAIL_OTP_AUTO_SUBMIT` remains `false` so the bot does not automatically submit a code to get past a site's verification or anti-bot step.

## Unattended use and consent status

Google classifies `gmail.readonly` as a restricted scope. If an app stores restricted-scope data on a server or transmits it, Google says a security assessment is required. Keep this as a local, single-user integration; do not send message contents or codes to hosted services or ntfy, and keep codes transient and out of logs. Confirm the requirements before hosting or distributing the app. Google's [personal-use exception](https://support.google.com/cloud/answer/13464323) allows some apps used by fewer than 100 people to remain unverified, but users can see an unverified-app warning. While an OAuth app is in **Testing**, test-user authorization and its refresh token expire after seven days, so Testing mode is unsuitable for unattended 24/7 use. See [Google's audience and publishing-status guidance](https://support.google.com/cloud/answer/15549945).

## Configuration file

Edit `config/gmail.env` to set the search label/query, then enable the sync after authorization. `GMAIL_SYNC_MAX_MESSAGES` defaults to 100, and `GMAIL_DB_PATH` defaults to `state/jobbot.sqlite3`. The sync stays disabled until `GMAIL_ENABLED=true`.
