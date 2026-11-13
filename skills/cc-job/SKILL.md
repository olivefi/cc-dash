---
name: cc-job
description: Tag this session in the cc-dash dashboard when it starts work that outlives the current turn — a GPU training or simulation run (usually in Docker) or a job submitted to the Euler cluster — and resolve the tag once its results are checked or it was cancelled. Use whenever you launch, finish or cancel such a job.
---

# Job tags for cc-dash

The user tracks many Claude sessions in cc-dash. Sessions with outstanding jobs are listed
at the top of its sidebar, so they can find the ones that still need resolving. Keep this
session's tags accurate with `cc-job` (it knows the session from `$CLAUDE_CODE_SESSION_ID`).

When you **start** a long-running job, tag it right after launching:

```sh
cc-job add gpu "train slope policy, 4500 iters" --for 3h
cc-job add euler "drop sweep 16 runs" --job 5551234 --for 8h
cc-job add gpu "sim recording" --pid 12345     # a local process: due once it exits
```

- `kind` is a short word (`gpu`, `euler`, ...); the description says what it is in a few words.
- Give `--for` with a realistic runtime estimate: when it runs out, the job shows as due and
  the user knows to check on it. Add `--pid` when the job is a local process you started.
- `sbatch` submissions are tagged automatically by a hook (named after `--job-name`). Run the
  same `cc-job add euler ... --job <id> --for <estimate>` afterwards to add a description and
  timer; it updates that tag instead of duplicating it.

When a job is **resolved** (you have looked at its results, it failed and you handled it, or
it was cancelled), remove its tag:

```sh
cc-job ls            # this session's jobs and their ids
cc-job done <id>     # one job
cc-job done          # all of this session's jobs
```

Do not poll the cluster just to keep tags fresh; check a job when the user asks or when its
timer ran out.
