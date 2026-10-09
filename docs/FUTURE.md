# Future work

Things we've decided to do later, with enough context to pick each one up cold. v1 scope is in [SPEC.md](SPEC.md);
this list covers what comes after, plus loose ends from building v1.

## Before v1 goes live

- **Staging run on the maintainer's machine** ([dev/staging.md](dev/staging.md)). The Compose stack itself has now
  been run with stub credentials (HANDOFF section 4), so what's left needs real accounts:
  - the SkyPilot server and oauth2-proxy with a real Weave sign-in;
  - a real Vast launch;
  - the 80% warning and 100% teardown with real spend;
  - how far `cost_report` drifts from the Vast bill. That sets the safety margin, if any, to take off the ceiling.
- **Run the live Weave role tests** against Weave `main` (`docs/dev/weave-e2e.md`).
  `scripts/dev/weave_e2e_provision.rb` now gives the fixture users Krater's app roles, but hasn't been run yet.
- **Set up Krater's roles in Weave** on each real deployment: a Weave superadmin creates `member`, `reviewer` and
  `admin` on the Krater app page, an admin adds the `directory` scope to the app, and admins give people the roles
  (see [weave-integration.md](weave-integration.md)).
- **Create Krater's Slack app** in the Patchwork workspace ([dev/slack-setup.md](dev/slack-setup.md)), and a tunnel
  for local testing.
- **Pick a long-term screenshot storage provider.** Today it's a temporary self-hosted SeaweedFS container. The options
  are R2/B2/S3, or keeping SeaweedFS with backups (a reminder was set for early October 2026).
- **Decide what happens exactly at the ceiling** (patchworklabsorg/krater#2). Today warnings go out at 80% and teardown happens at 100% with no
  grace period, which can kill a long training run just past the limit. Consider a small admin-configurable grace margin.

## Roles and accounts

- **Cache Slack email-lookup misses.** Krater calls `users.lookupByEmail` for anyone without a stored Slack id each
  time it builds a channel's invite list (misses aren't cached). Fine at Ganymede's size; cache misses for a while if
  Slack rate limits show up.

## Weave follow-ups

- **[weave#118](https://github.com/patchworklabsorg/weave/issues/118): require joining Slack as part of membership.**
  Krater's gate uses Weave's `slack_member` when Weave sends it, and asks Slack otherwise. *Requiring* Slack as part
  of signing up is still a Weave product decision.
- **Weave admin loose ends** (found while fixing Weave's admin users privilege bug, see HANDOFF section 3): there's no
  `admin/users/new` view in Weave, so creating a user from Weave's admin panel errors.
- **Reviewer tiers need Weave role keys.** `ganymede:reviewer:<tier>` has no Weave role key yet. Add one to the
  Krater app in Weave and to `krater.weave.roles` before using tiered approval policies.

## Product features parked in the spec

- **In-progress project visibility:** let members see active, not-yet-completed projects, to avoid duplicate work.
- **Multi-approval and reviewer tiers by budget size.** Already supported by `ApprovalPolicy` rows; this is a policy
  decision plus a small admin UI to edit the rows (today they're read-only in `/admin`).
- **Mirroring Slack comments into Krater,** if linking to the channel turns out not to be enough.
- **Reconciling spend against real Vast billing** instead of SkyPilot's catalog-based estimate.

## SkyPilot

- **Untested with 0.13.0's CLI:** `sky exec`, `sky jobs launch` and `sky serve up` going through the launch gate for
  real. They have no `--dryrun`, so they need a real Vast key. Resource labels on Vast are also untested. See
  [dev/skypilot-spike.md](dev/skypilot-spike.md).
- **Image allowlist in the launch gate,** to stop crypto mining or other abuse. It becomes more important if donated
  compute happens (below).
- **Shared rate limiting.** The limiter is per-process in memory, so limits multiply with the number of web workers. Move
  it to Postgres if abuse shows up.

## Donated idle compute (idea, parked)

Let members donate idle GPU time on their own machines to Ganymede through SkyPilot.

**Feasible with SkyPilot's existing pieces.** SkyPilot treats a Kubernetes cluster as another infrastructure. Its SSH
Node Pools feature (`sky ssh up`) turns existing machines into one using **k3s** (confirmed in SkyPilot's source).
Project workspaces could allow a "donated" Kubernetes pool alongside Vast. Donated nodes cost $0 to the optimizer by
default, so SkyPilot would prefer them and fall back to Vast.

**Proposed shape**
- **One Krater-run k3s cluster; donors join as worker nodes** over a private mesh such as Tailscale or Headscale. Home
  machines sit behind NAT, and k3s needs the server and agents to reach each other.
- **Pause and resume rather than boot and kill.** The k3s agent stays installed. A small donor-side agent (a systemd
  service) watches for idleness: no input for N minutes, GPU unused, optionally on AC power, within donor-set hours.
  When idle, it **uncordons** the node. When the owner returns, it **cordons and drains** it. This is more reliable than
  joining and leaving the cluster repeatedly.
- **Treat donated nodes like spot instances.** Drains evict jobs, so only SkyPilot **managed jobs** (which auto-recover,
  including onto Vast) should run there, and they must checkpoint, for example to the S3 storage. The launch gate can
  enforce "donated pool ⇒ managed jobs only".
- **Credit donors.** Track donated GPU-hours per donor through node labels; that's a good line in gallery credits.

**Open problems to decide first**
1. **Trust runs both ways.** Donors run other members' code (containers aren't a strong boundary on a GPU box), and
   donors with root can see a job's data and models. Only viable among vetted members. It helps that completed
   projects are meant to be open source anyway.
2. **Windows donors are hard.** k3s is Linux. GPU access in WSL2 exists, but k3s plus the NVIDIA device plugin inside
   WSL2 is fiddly. A pilot should be Linux-only.
3. **Heterogeneous hardware and home bandwidth.** Mixed GPUs and drivers, and slow downloads of images and datasets.
   Suited to small, single-node jobs; no multi-node training.
4. **Budget accounting.** Donated compute is $0 to SkyPilot, so Krater's dollar budgets wouldn't count it. Either
   donated compute is free (budgets cover Vast only), or it's charged at an internal rate.
5. **Abuse and donor cost.** Crypto mining, and electricity. Needs an image allowlist in the launch gate and donor-set
   caps and hours.

**Suggested first step:** a pilot with 2–3 trusted Linux GPU machines on Tailscale, the cordon/uncordon agent, and
managed jobs with checkpointing. Before that, do a quick local check that SkyPilot's Kubernetes support handles
cordon/drain and managed-job recovery the way this needs.
