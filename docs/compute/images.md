# Container images

Members name a public container image for each job or box (`ghcr.io/me/train:v3`, `ubuntu:24.04`). Krater doesn't
store images: the rented machine pulls the image from its registry. What Krater does with images:

## Checked when the job is queued

The form, the API's submit and its estimate (so the CLI too) check the image before anything is queued
(`krater.services.compute_jobs.image_error` and `check_image`):

- **The name.** A web address is refused, naming the image when it's a Docker Hub or GitHub package page
  (`https://hub.docker.com/r/pytorch/pytorch` → `pytorch/pytorch`); a pasted `docker pull X` gets `X`; anything else
  must parse as Docker does (`krater.registry.parse_reference`).
- **The registry's answer.** `resolve_digest` (below): a 404, or a 401 or 403 to an anonymous token, is
  `ImageUnavailable`, and the job is refused with the registry's words. In production (2026-11-12) a box named the
  guide's example `ghcr.io/you/train:v1` with a full stop after it, and the scheduler rented three machines for it.
  A registry that can't be asked (down, 429, a host Krater won't reach) lets the job through: the scheduler then
  fails it as soon as a machine's pull is refused (`docs/compute/scheduler.md`). The app keeps one registry client,
  so an expired anonymous token is replaced, not taken for a refusal. The shell submitter (`krater.compute.submit`)
  doesn't ask.

The form starts with `compute_jobs.DEFAULT_IMAGE` (`pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`), a public image
with a shell and Python, so the example on the form works as it is.

## The digest each machine got

When the scheduler rents a machine it asks the image's registry what the tag points to now
(`krater.registry.resolve_digest`: a HEAD request for the manifest, with an anonymous token, which Docker Hub doesn't
count against its pull limit). It records the digest on the instance (`compute_instances.image_digest`), at most one
lookup per image per scheduler pass. A registry that can't answer doesn't hold the rental up: the machine gets the tag
and no digest is recorded.

- **Pinning off** (the default): the machine is launched with the tag, and pulls it a moment later. That's normally
  the digest Krater recorded, but a tag pushed in those seconds could differ, and the pages say "looked up at rental".
- **Pinning on** (`KRATER_COMPUTE_PIN_IMAGE_DIGEST=true`): the machine is launched with `name@sha256:...`, so it runs
  exactly the recorded digest (`compute_instances.image_pinned`). Every attempt looks the tag up again, so a job that
  moves to another machine after the member pushed a new image gets the new one.

### Before turning pinning on

Vast hasn't yet been shown to pull an image by digest reference. One short paid rental on staging checks it (a few
cents, with Adam's OK first):

1. Set `KRATER_COMPUTE_PIN_IMAGE_DIGEST=true` on staging, with compute on.
2. Submit a small job with a multi-platform public image, such as `ubuntu:24.04` with the command `cat /etc/os-release`.
3. Check the job runs and succeeds, the job page says "pinned", and Vast's instance shows the `@sha256:` image.
4. If Vast refuses or fails the pull, turn the setting off again: nothing else depends on it.

## Admin review (`/admin/images`)

Admins see every image members have named, with each digest machines got and how many. **Inspect** asks the registry
for the linux/amd64 image's config and stores what it said (`ImageInspection`): entrypoint, command, user, working
directory, environment, labels, exposed ports, size, and the build history (each Dockerfile step as the image records
it). Inspect a digest to see what a job ran; inspecting a tag shows what it points to now. A job's page shows its
machine's digest, with an Inspect button for admins. Krater replaces the entrypoint with its bootstrap either way.

Images are public only: Krater has no registry logins, so a private image can't be inspected, and couldn't be pulled
by the machine either.

## Safety

An image reference can name any host, and Krater runs where it can reach internal services. So `krater.registry`,
the only code that talks to registries:

- uses HTTPS only;
- refuses a host that is, or resolves to, a loopback, private, link-local or other non-public address, and names such
  as `localhost` or `*.internal`; it checks the registry, its token server, and every redirect (blobs redirect to a
  CDN) one hop at a time, and never sends the registry's token to a redirect;
- checks the manifest and config against their digests;
- caps every answer (1 MB for a manifest, 8 MB for a config) and times out after 10 seconds.
