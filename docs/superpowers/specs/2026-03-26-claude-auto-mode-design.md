# Design: Claude Auto Mode for Blog Generation Scripts

**Date:** 2026-03-26
**Status:** Approved

## Summary

Add `--dangerously-skip-permissions` to the Claude CLI subprocess calls in `scripts/post_generator.py` and `scripts/topic_generator.py` so that blog generation runs fully unattended without permission prompts blocking execution.

## Problem

When the blog generation pipeline runs on a cron schedule, any interactive permission prompt from `claude -p` will stall the process indefinitely. The existing `--allowedTools` flag in `post_generator.py` whitelists tools but does not suppress the per-use permission prompts that Claude CLI may raise during tool invocations.

## Approach

**Option chosen:** Add `--dangerously-skip-permissions` to both generators, keeping `--allowedTools` in `post_generator.py` as a safety layer (belt-and-suspenders).

- `--dangerously-skip-permissions` skips all permission prompts — unblocks cron/automated runs
- `--allowedTools` in `post_generator.py` remains in place — restricts which tools Claude can invoke during post generation
- `topic_generator.py` intentionally has no `--allowedTools` — topic generation only produces JSON text and invokes no tools; no allowlist is needed or appropriate there

## Changes

### `scripts/post_generator.py`

Add `"--dangerously-skip-permissions"` to the `subprocess.run` argument list, inserted after `"--output-format", "text"` and before `"--allowedTools", allowed`.

**Before:**
```python
result = subprocess.run(
    [
        "claude", "-p", prompt,
        "--output-format", "text",
        "--allowedTools", allowed,
    ],
    capture_output=True,
    text=True,
    timeout=timeout,
    cwd=str(config.BLOG_DIR),
)
```

**After:**
```python
result = subprocess.run(
    [
        "claude", "-p", prompt,
        "--output-format", "text",
        "--dangerously-skip-permissions",
        "--allowedTools", allowed,
    ],
    capture_output=True,
    text=True,
    timeout=timeout,
    cwd=str(config.BLOG_DIR),
)
```

### `scripts/topic_generator.py`

Add `"--dangerously-skip-permissions"` to the `subprocess.run` argument list, appended after `"--output-format", "text"`. No `--allowedTools` is added — topic generation is text-only and requires no tools.

**Before:**
```python
result = subprocess.run(
    ["claude", "-p", prompt, "--output-format", "text"],
    capture_output=True,
    text=True,
    timeout=config.TIMEOUT_TOPIC_GENERATION,
    cwd=str(config.BLOG_DIR),
)
```

**After:**
```python
result = subprocess.run(
    ["claude", "-p", prompt, "--output-format", "text", "--dangerously-skip-permissions"],
    capture_output=True,
    text=True,
    timeout=config.TIMEOUT_TOPIC_GENERATION,
    cwd=str(config.BLOG_DIR),
)
```

**Note on retry behavior:** `topic_generator.py` has a two-attempt retry loop. Previously a stalled permission prompt could cause a non-zero exit and trigger the retry. After this change, that failure mode is eliminated; the retry loop remains in place for other failure modes (e.g. malformed JSON output).

### Files not changed

- `scripts/config.py` — `ALLOWED_TOOLS_DIAGRAM` / `ALLOWED_TOOLS_TEXT` constants unchanged
- `scripts/orchestrator.py` — no changes
- `scripts/run_blog.sh` — no changes

## Trade-offs

| Concern | Mitigation |
|---|---|
| Claude could run arbitrary tools in post generation | `--allowedTools` still enforces the tool whitelist in `post_generator.py` |
| Claude could run arbitrary tools in topic generation | Topic generation prompt requests only JSON text; no tool use expected or observed |
| Prompt injection via generated content | Prompts are constructed from trusted internal data (topic dict, config) |
| Cron stalls on permission prompt | Eliminated by this change |

## Success Criteria

1. `orchestrator.py` completes a full run without any interactive prompts
2. **Verification of tool allowlist:** Inspect the final `subprocess.run` call in `post_generator.py` and confirm both `"--dangerously-skip-permissions"` and `"--allowedTools", allowed` are present in the argument list. Running `python3 -c "import post_generator"` with a mock confirms no import errors.
3. No changes to post quality, gist embedding, or git publish behavior
