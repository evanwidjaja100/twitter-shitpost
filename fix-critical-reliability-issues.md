# Fix Two Critical Reliability Issues

You are working on the Python repository `twitter-shitpost-main`.

Fix the following two critical reliability issues without changing unrelated behaviour.

---

## Issue 1 — Incorrect Playwright Timeout Units

### Relevant file

`publisher/x_publisher.py`

The publisher uses parameters named `timeout_s`, but passes their values directly to Playwright methods such as:

- `locator.wait_for(...)`
- `locator.click(...)`
- Any other Playwright operation that accepts a timeout

Playwright expects timeout values in **milliseconds**, while the current values are expressed in **seconds**.

For example, `timeout_s=60` currently results in approximately 60 milliseconds rather than 60 seconds.

### Required changes

1. Keep the public configuration and function parameters expressed in seconds.
2. Convert seconds to milliseconds exactly once before passing the value to Playwright:

```python
timeout_ms = max(1, int(timeout_s * 1000))
```

3. Audit the entire `publisher/x_publisher.py` file for every Playwright timeout.
4. Ensure no value named `timeout_s` is passed directly into Playwright.
5. Explicitly focus or click the post composer before entering text.
6. Prefer locator-level text entry such as `fill()` or `press_sequentially()` rather than sending keyboard input to the page without confirming focus.
7. Preserve the existing humanised typing behaviour where practical, but reliability is more important than simulated typing.
8. Do not add arbitrary sleeps as the main fix.
9. Preserve existing return structures and error handling unless a change is required to fix the defect.

### Acceptance criteria

- A configured timeout of `60` seconds is passed to Playwright as `60000` milliseconds.
- The composer is explicitly focused before text is entered.
- Text cannot accidentally be typed into the wrong page element.
- No Playwright call receives seconds where milliseconds are expected.
- Existing publisher callers do not need to change.

---

## Issue 2 — Content Is Deduplicated Before Publishing Succeeds

### Relevant files

Likely files include:

- `main.py`
- `storage/db.py`
- Any models or helper modules involved in candidate selection and publishing

Currently, `pick_item()` records the source ID and media hash before `session.post()` completes.

When publishing fails, the content is still considered used and will not be retried.

### Required behaviour

The desired sequence is:

```text
Select candidate
→ prepare media
→ attempt publication
→ if publication succeeds, record source ID and hash
→ if publication fails, do not permanently deduplicate it
```

### Required changes

1. Remove permanent deduplication writes from `pick_item()` or any other pre-publication selection path.
2. `pick_item()` should only select and return a candidate.
3. Call `db.record_source(...)` and `db.record_hash(...)` only after `session.post(...)` returns a confirmed successful result.
4. Apply the fix to every publishing path, including:
   - One-off or manual execution
   - Daemon or scheduled execution
   - Any retry or alternate posting path
5. Use one shared helper for success handling so manual and daemon flows cannot diverge.
6. Do not record successful deduplication state when:
   - Media preparation fails
   - Browser startup fails
   - Authentication fails
   - Upload fails
   - The post button cannot be clicked
   - The publisher returns `ok=False`
   - An exception occurs before success is confirmed
7. Preserve the existing database schema unless a schema change is truly necessary.

### Recommended structure

A structure similar to this is acceptable:

```python
def mark_item_published(db, item) -> None:
    db.record_source(
        source=item.source,
        source_id=item.source_id,
        url=item.url,
    )

    if item.media_hash:
        db.record_hash(item.media_hash)
```

Then:

```python
result = session.post(...)

if result.get("ok") is True:
    mark_item_published(db, item)
else:
    logger.warning(
        "Post failed; candidate remains eligible for retry: %s",
        result.get("reason"),
    )
```

Adapt field names to the repository's actual data structures. Do not invent fields that are not available.

### Atomicity

Where practical, save the successful source ID and successful media hash inside one SQLite transaction.

A partial write must not leave the database in an inconsistent state.

Avoid this outcome:

```text
source ID saved
→ process crashes
→ media hash not saved
```

Add a database method such as:

```python
db.record_successful_item(...)
```

This method should perform all required success writes in one transaction.

### Concurrency consideration

Do not create a large queue system as part of this task.

However, ensure that moving deduplication writes does not introduce obvious duplicate writes or database errors.

If the database has uniqueness constraints, handle duplicate insertions safely and idempotently.

---

## Tests

Add automated tests for both fixes.

Use the project's existing test framework. If no framework exists, add lightweight `pytest` tests.

### Timeout tests

Test that:

1. `timeout_s=60` becomes `60000`.
2. Playwright methods receive milliseconds.
3. The composer is clicked or focused before text entry.
4. The test uses mocks and does not require a real X account or browser session.

### Deduplication tests

Test the following scenarios.

#### Successful publication

```text
candidate selected
→ session.post returns {"ok": True}
→ source and media hash are recorded
```

#### Failed publication

```text
candidate selected
→ session.post returns {"ok": False}
→ source and media hash are not recorded
```

#### Exception during publication

```text
candidate selected
→ session.post raises an exception
→ source and media hash are not recorded
```

#### Media preparation failure

```text
candidate selected
→ media preparation fails before session.post
→ source and media hash are not recorded
```

#### Atomic database recording

Verify that successful source and hash records are committed together.

---

## Validation Steps

After implementing the changes:

1. Run the complete test suite.
2. Run Python syntax or compilation validation.
3. Search the repository for all calls to:
   - `record_source`
   - `record_hash`
   - `session.post`
   - Playwright `timeout=`
4. Confirm no pre-publication path still permanently marks content as used.
5. Confirm all Playwright timeout values use milliseconds.
6. Report every modified file.
7. Provide a concise explanation of:
   - The original defects
   - The implementation
   - The tests added
   - Any remaining limitations

---

## Constraints

- Make the smallest coherent patch.
- Do not rewrite unrelated modules.
- Do not change scraping, scoring, caption generation, or scheduling behaviour.
- Do not expose credentials, browser profile contents, cookies, or tokens.
- Do not weaken existing error handling.
- Do not claim success unless the tests actually pass.
