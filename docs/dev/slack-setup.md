# Slack app setup (dev)

Krater gets **its own Slack app** for Project Ganymede's review workflow -- see `docs/SPEC.md` "Slack
integration". This is a from-scratch setup for the Patchwork Labs workspace, plus how to drive it against
a Krater running locally on the maintainer's Windows machine (see `docs/dev/staging.md` for the rest of
that setup). Read `docs/SPEC.md` "Slack integration" first; this doc is just the mechanics.

## 1. Create the app

1. [api.slack.com/apps](https://api.slack.com/apps) -> **Create New App** -> **From scratch**, in the
   Patchwork Labs workspace. Name it something recognizable in a channel list, e.g. "Ganymede Review"
   (this is *not* Weave's Slack app -- Weave already uses the workspace's one interactivity URL for
   code-of-conduct acceptance, so Krater needs its own app entirely).
2. **OAuth & Permissions** -> **Scopes** -> **Bot Token Scopes**, add exactly what `docs/SPEC.md` lists:
   - `groups:write` -- create/archive private channels
   - `groups:write.invites` -- invite the submitter, credited builders and reviewers
   - `chat:write` -- post/update the review message and admin-override notices
   - `users:read` -- look up a user's guest/restricted/deleted status (`users.info`) for the Slack membership
     gate, when Weave doesn't send `slack_member`
   - `users:read.email` -- Krater finds a person's Slack account with `users.lookupByEmail` (using the email
     Weave verified) when Weave has no `slack_id` for them, for the membership gate and for channel invites
3. **Install App** (or **Install to Workspace**) at the top of that same page. Copy the **Bot User OAuth
   Token** (`xoxb-...`) -> `KRATER_SLACK_BOT_TOKEN`.
4. **Basic Information** -> **App Credentials** -> copy the **Signing Secret** ->
   `KRATER_SLACK_SIGNING_SECRET`. This is what `krater.slack.signature.verify_slack_signature` checks
   every `/slack/interactions` request against -- treat it like any other secret.
5. **Interactivity & Shortcuts** -> turn **Interactivity** on -> **Request URL**:
   `https://<public>/slack/interactions` (see "Tunneling" below for what `<public>` is locally; in
   staging/production it's the portal's real public hostname). This is the one interactivity URL the app
   gets, and it's the only endpoint Slack calls for both button clicks and modal submissions.
6. Invite the bot to a channel you'll use as the master feed (`docs/SPEC.md`: "one top-level post per new
   submission"), then get that channel's id (right-click it in Slack -> **View channel details** -> the
   id is at the bottom, `C0123456789`) -> `KRATER_SLACK_FEED_CHANNEL_ID`.

The bot does **not** need to be manually invited to project channels -- it creates them itself
(`conversations.create`) and is a member from the start.

## 2. Env vars

```
KRATER_SLACK_MODE=live
KRATER_SLACK_BOT_TOKEN=xoxb-...
KRATER_SLACK_SIGNING_SECRET=...
KRATER_SLACK_FEED_CHANNEL_ID=C0123456789
KRATER_SLACK_RECONCILE_INTERVAL_MINUTES=10   # default; how often the worker's periodic slack_reconcile runs
```

`KRATER_SLACK_MODE` defaults to `fake` (an in-memory Slack, `krater.slack.FakeSlackClient` -- no real
workspace needed for ordinary dev/tests), and the app refuses to start with `fake` when
`KRATER_ENV=production` (mirrors `KRATER_SKYPILOT_MODE`/`KRATER_WEAVE_MODE`). Set `KRATER_SLACK_MODE=live`
only once you actually want Krater talking to the real workspace above.

## 3. Tunneling from the maintainer's Windows machine

Slack has to reach `/slack/interactions` over public HTTPS, so a local Krater (per `docs/dev/staging.md`,
running under Docker Desktop/WSL2) needs a tunnel. Either works; both are run from **PowerShell** (not
WSL2 -- they need to reach `localhost:8000`, which Docker Desktop's WSL2 integration already exposes to
Windows):

**Cloudflare Tunnel** (no account needed for a quick, temporary tunnel):

```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

**ngrok** (needs a free account/authtoken):

```powershell
winget install ngrok.ngrok
ngrok config add-authtoken <your-authtoken>
ngrok http 8000
```

Either prints a `https://<random>.trycloudflare.com` or `https://<random>.ngrok-free.app` URL. Use it as:

- `KRATER_BASE_URL` (so Weave's OAuth redirect and any absolute links Krater generates are correct), and
- the Slack app's **Interactivity & Shortcuts** Request URL, `https://<that-url>/slack/interactions`.

The tunnel's URL changes every time you restart it (free Cloudflare/ngrok tunnels aren't stable), so
update the Slack app's Request URL each time before testing interactions again. Slack's interactivity
requests are otherwise ordinary HTTPS POSTs -- no special tunnel configuration is needed beyond exposing
the port.

## 4. Trying it out

With `KRATER_SLACK_MODE=live` and the tunnel up:

1. Submit a proposal as a member with a real Slack account in the workspace, under the same email as their
   Weave account (or with Slack linked in Weave, which sends `slack_id`). The membership gate in
   `krater.services.slack_membership` uses Weave's `slack_member` when Weave sends it. Otherwise it finds the
   account by stored id or `users.lookupByEmail`, then requires `users.info` to show a non-guest,
   non-deleted account -- see `docs/SPEC.md` "Roles & authentication".
2. Krater should create a private `ganymede-<slug>-<id>` channel, invite you and every current
   reviewer (Weave's directory lists everyone with the Krater `reviewer` role), and post the review message with
   Approve/Reject buttons.
3. Click **Reject** -> a modal should open asking for a reason (this is the one interaction Krater
   answers synchronously, since the modal's `trigger_id` expires in ~3s -- everything else is a
   deferred `procrastinate` job, so `uv run procrastinate --app=krater.worker.app.app worker` must
   actually be running for anything past the initial ack to happen).
4. Submitting the modal, or clicking **Approve** as a *different* Slack user who's a current reviewer,
   should update the message to show the outcome and drop the buttons.

If nothing happens past the initial click, check the worker's logs first -- the interactivity route
itself only acks and defers; `krater.services.slack_reviews`/`krater.services.slack_notify` do the actual
work, and log (`logger.exception`) rather than raise on a Slack-side failure.
