# Krater guides

These guides are also Krater's **Help** pages: open **Help** in Krater's top bar, or go to `/help`. Krater renders
these same files, so edit them here.

For members:

- [Using Krater](using-krater.md): applying, your team, review, budgets, amendments, the final write-up and the
  gallery.
- [Running jobs and boxes](compute-jobs.md): preparing an image, submitting a job, choosing a price, results, boxes,
  secrets and the `krater` command.
- [Run only when compute is cheap](run-when-cheap.md): make a job wait for a GPU price you choose.

For reviewers and admins:

- [Reviewing and running Krater](reviewing-and-admin.md): reviewing, granting less, funding holds, overrides,
  compute settings and alerts.
- [Record and report an OSPool grant](ospool-grants.md)
- [Share proposals with OSPool](share-with-ospool.md)

To add a guide to Help, list it in `GUIDES` in `krater/web/help_docs.py`. Link between guides by file name
(`compute-jobs.md#secrets`); Krater turns those into Help links, and links to other docs here into GitHub links.
`tests/web/test_help.py` checks every link and heading anchor.
