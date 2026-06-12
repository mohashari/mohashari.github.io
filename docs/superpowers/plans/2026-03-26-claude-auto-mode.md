# Claude Auto Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--dangerously-skip-permissions` to all `claude -p` subprocess calls so the blog generation pipeline runs fully unattended without permission prompts blocking cron execution.

**Architecture:** Two surgical one-line edits — one in `post_generator.py` and one in `topic_generator.py`. The flag is appended to the existing argument lists. `post_generator.py` retains `--allowedTools` alongside the new flag (belt-and-suspenders). `topic_generator.py` gets only the new flag (no tools are invoked during topic generation).

**Tech Stack:** Python 3, `subprocess.run`, Claude CLI (`claude -p`), pytest

---

## File Map

| File | Change |
|---|---|
| `scripts/post_generator.py` | Add `"--dangerously-skip-permissions"` to subprocess args |
| `scripts/topic_generator.py` | Add `"--dangerously-skip-permissions"` to subprocess args |
| `scripts/tests/test_post_generator.py` | Add test asserting flag is present in call args |
| `scripts/tests/test_topic_generator.py` | Add test asserting flag is present in call args |

---

## Task 1: Auto mode for `post_generator.py`

**Files:**
- Modify: `scripts/post_generator.py:98-108`
- Test: `scripts/tests/test_post_generator.py`

- [ ] **Step 1: Write the failing test**

Add this test to the bottom of `scripts/tests/test_post_generator.py`:

```python
def test_generate_includes_dangerously_skip_permissions():
    gen = post_generator.PostGenerator(logger)
    mock_result = MagicMock(returncode=0, stdout=VALID_POST, stderr="")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        gen.generate(SAMPLE_TOPIC, "2026-03-23")
    args = mock_run.call_args[0][0]  # command list is first positional arg
    assert "--dangerously-skip-permissions" in args
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_post_generator.py::test_generate_includes_dangerously_skip_permissions -v
```

Expected: `FAILED` — `AssertionError` because the flag is not yet in the call args.

- [ ] **Step 3: Add the flag to `post_generator.py`**

In `scripts/post_generator.py`, locate the `subprocess.run` call (around line 98). Add `"--dangerously-skip-permissions"` after `"--output-format", "text"` and before `"--allowedTools", allowed`:

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

- [ ] **Step 4: Run the full post_generator test suite**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_post_generator.py -v
```

Expected: All tests `PASSED`. No regressions.

- [ ] **Step 5: Commit**

```bash
cd /home/muklis/Documents/exploring/blog
git add scripts/post_generator.py scripts/tests/test_post_generator.py
git commit -m "feat: add --dangerously-skip-permissions to post_generator claude call"
```

---

## Task 2: Auto mode for `topic_generator.py`

**Files:**
- Modify: `scripts/topic_generator.py:64-70`
- Test: `scripts/tests/test_topic_generator.py`

- [ ] **Step 1: Write the failing test**

Add this test to the bottom of `scripts/tests/test_topic_generator.py`:

```python
def test_generate_includes_dangerously_skip_permissions():
    gen = topic_generator.TopicGenerator(logger)
    mock_result = MagicMock(returncode=0, stdout=json.dumps(VALID_TOPICS), stderr="")
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        gen.generate(past_slugs=[])
    args = mock_run.call_args[0][0]  # command list is first positional arg
    assert "--dangerously-skip-permissions" in args
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_topic_generator.py::test_generate_includes_dangerously_skip_permissions -v
```

Expected: `FAILED` — `AssertionError` because the flag is not yet in the call args.

- [ ] **Step 3: Add the flag to `topic_generator.py`**

In `scripts/topic_generator.py`, locate the `subprocess.run` call inside the `generate` method (around line 64). Append `"--dangerously-skip-permissions"` to the argument list:

```python
result = subprocess.run(
    ["claude", "-p", prompt, "--output-format", "text", "--dangerously-skip-permissions"],
    capture_output=True,
    text=True,
    timeout=config.TIMEOUT_TOPIC_GENERATION,
    cwd=str(config.BLOG_DIR),
)
```

- [ ] **Step 4: Run the full topic_generator test suite**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/test_topic_generator.py -v
```

Expected: All tests `PASSED`. No regressions.

- [ ] **Step 5: Commit**

```bash
cd /home/muklis/Documents/exploring/blog
git add scripts/topic_generator.py scripts/tests/test_topic_generator.py
git commit -m "feat: add --dangerously-skip-permissions to topic_generator claude call"
```

---

## Final Verification

- [ ] **Run the full test suite**

```bash
cd /home/muklis/Documents/exploring/blog/scripts
python3 -m pytest tests/ -v
```

Expected: All tests pass across all modules.
