# Autonomous Repair Plan — Safe Keyboard Activation Fallback for X Post Button When a Transparent Overlay Intercepts Pointer Events

You are working on the **latest repository state**.

The X publisher is now successfully reaching all of these states in real production:

```text
browser context recovery
→ source scraping
→ candidate preparation
→ X compose
→ caption entered
→ video attached
→ video reports Ready
→ active visible compose identified
→ correct Post button identified
→ Post button visible
→ Post button enabled
→ Post button stable
```

However, multiple real unattended attempts fail at the final physical mouse click.

The captured Playwright error now conclusively identifies the blocker:

```text
element is visible, enabled and stable
scrolling into view if needed
done scrolling

<div ...></div> ... subtree intercepts pointer events
```

The publisher's own hit-test diagnostics confirm:

```text
pointer_hit.inside_post_button = False
```

and the top hit-tested element is an otherwise non-semantic `div` overlay.

A real failure screenshot shows:

```text
video status: Ready
caption present
active compose open
correct bottom-right Post button visible
correct Post button visually enabled
no visible user-facing modal covering the button
```

Therefore, the remaining defect is specifically:

```text
correct enabled Post button
+
transparent/non-semantic X layer intercepts mouse pointer events
→ Playwright normal click cannot reach button
```

The existing normal mouse-click behavior, 15-second click timeout, diagnostics, and post-click reconciliation must remain.

The new task is to add a **tightly guarded keyboard activation fallback** for this one proven failure class.

---

# 1. CRITICAL SAFETY MODEL

Do NOT implement:

```python
post_button.click(force=True)
```

Do NOT implement:

```javascript
element.click()
```

Do NOT delete/hide the intercepting DOM element.

Do NOT remove:

```text
pointer-events
disabled
aria-disabled
overlay DOM
```

Do NOT click raw screen coordinates.

Do NOT automatically try multiple activation methods.

Instead, the fallback should use the real enabled HTML button's normal keyboard activation semantics.

Conceptual safe fallback:

```text
normal mouse click attempted
→ mouse click times out
→ reconcile whether X already posted
→ prove timeout was specifically pointer interception
→ freshly rediscover active compose
→ freshly rediscover correct Post button
→ revalidate all post state
→ focus that exact verified button
→ verify keyboard focus belongs to that button
→ press Enter exactly once
→ require explicit "Your post was sent"
```

This is not a replacement for the normal mouse click.

It is a narrow fallback for a specific proven actionability failure.

---

# 2. AUTONOMOUS WORKFLOW

Inspect the current code before making any changes.

Set your own concrete engineering goals.

Then iterate autonomously:

```text
inspect
→ define goals
→ reproduce real pointer-interception failure deterministically
→ inspect current click-timeout reconciliation
→ implement timeout classification
→ implement guarded keyboard fallback
→ add regression tests
→ run focused tests
→ independently reproduce mouse-blocked / keyboard-success case
→ independently reproduce ambiguous mouse-success case
→ self-review duplicate-post risk
→ run full safe suite
→ repeat until every acceptance criterion passes
```

Do not stop just because:

```text
Enter successfully submits a fake button
```

The difficult part is proving that Enter is only used when it is safe.

---

# 3. ENGINEERING GOALS

Set your own goals after inspection.

They should include at least:

```text
Goal 1:
Keep normal Playwright mouse clicking as the primary Post action.

Goal 2:
Classify click timeouts and distinguish proven pointer interception
from other Playwright timeout/actionability failures.

Goal 3:
Never keyboard-activate after an ambiguous timeout until the existing
positive-success reconciliation has completed.

Goal 4:
Use keyboard fallback only when evidence indicates the mouse action
was prevented before reaching the Post button.

Goal 5:
Freshly rediscover and revalidate the active compose/button before
keyboard activation.

Goal 6:
Verify focus actually belongs to the intended Post button before
sending Enter.

Goal 7:
Send exactly one keyboard activation.

Goal 8:
Still require the explicit "Your post was sent" signal for success.

Goal 9:
Preserve dedup/database safety and prevent duplicate publication.
```

Report your actual goals at completion.

---

# 4. STRICT SCOPE

Do NOT modify:

```text
FFmpeg
yt-dlp
video encoding
media preparation
image/video readiness timeouts
scrapers
candidate scoring
scheduler
daemon posting windows
daily quotas
retention
database schema
dedup algorithms
browser profile locking
browser restart logic
active-compose ownership architecture
success-precedence fix
15-second mouse-click timeout
```

unless a directly necessary dependency is found.

The real production evidence already proves those components reach the correct pre-click state.

---

# ============================================================

# PART A — CLASSIFY THE MOUSE CLICK FAILURE

# ============================================================

# 5. DO NOT KEYBOARD-FALLBACK ON EVERY PlaywrightTimeoutError

A click timeout could mean:

```text
pointer interception
element instability
element detached
page closed
navigation
browser context failure
button stopped being enabled
unknown actionability failure
```

Keyboard fallback is **not appropriate for all of these**.

Add a narrow classification helper.

Conceptually:

```python
def _classify_post_click_timeout(exc, pointer_hit, ...):
    ...
```

Possible outcome values:

```text
pointer_intercepted
detached
unstable
context_closed
unknown
```

Do not overengineer the enum if simple constants are sufficient.

---

# 6. REQUIRED EVIDENCE FOR POINTER-INTERCEPTION FALLBACK

Keyboard fallback should require multiple consistent signals.

At minimum:

```text
A. Playwright exception indicates pointer interception
AND
B. fresh button remains visible
AND
C. fresh button remains enabled
AND
D. fresh button belongs to current active compose
AND
E. pointer hit-test says the top element is outside the Post button
```

The live exception contains text similar to:

```text
intercepts pointer events
```

Use a bounded robust classifier around the actual Playwright exception text.

Do not depend on the X-generated CSS class names:

```text
r-1p0dtai
r-ipm5af
...
```

Those classes are unstable implementation details.

---

# 7. DO NOT ASSUME THE MOUSE CLICK WAS DEFINITELY NOT DELIVERED

Even when pointer interception appears likely, preserve the existing ambiguity safety.

The correct order after `click()` raises timeout is:

```text
1. capture diagnostics
2. run existing short post-click reconciliation
3. if "Your post was sent" appears:
       return posted
       STOP
4. only if reconciliation confirms no success:
       consider keyboard fallback
```

This ordering is mandatory.

It prevents:

```text
mouse click actually posted
→ keyboard fallback posts again
```

---

# ============================================================

# PART B — PRESERVE THE EXISTING 5-SECOND RECONCILIATION

# ============================================================

# 8. RECONCILE FIRST

The repository already has a bounded reconciliation path after a mouse-click timeout.

Keep it.

The intended sequence is:

```text
normal click raises timeout
↓
5-second reconciliation
↓
positive "Your post was sent"?

YES:
    verified posted
    NO keyboard activation

NO:
    inspect timeout classification
```

Do not shorten this to zero.

Do not extend it to 60/180 seconds.

---

# 9. POSITIVE SUCCESS STILL WINS

Preserve the recently corrected post-click precedence:

```python
if positive_success:
    posted

elif problem:
    failure
```

A stale generic X error must not override:

```text
Your post was sent
```

Do not regress this.

---

# ============================================================

# PART C — REVALIDATE EVERYTHING BEFORE KEYBOARD FALLBACK

# ============================================================

# 10. FRESH DOM DISCOVERY AGAIN

Do not reuse the mouse-click locator after its 15-second timeout.

Immediately before keyboard activation:

```text
find fresh active visible composer
→ derive fresh active compose root
→ find fresh Post candidates within it
→ select unique correct Post button
```

The previously added compose ownership rules must remain authoritative.

---

# 11. REVALIDATE POST STATE

Before keyboard fallback require:

```text
composer still present
caption still present if requested
attachment still present
video still Ready where video state is applicable
no secondary media editor unresolved
Post button visible
Post button enabled
Post button belongs to active compose
no login redirect
no captcha
no explicit media error
no known blocking X problem
```

If any of those checks fails:

```text
do not keyboard activate
```

Return the appropriate existing safe failure.

---

# 12. POINTER INTERCEPTION MUST STILL BE PRESENT

Re-run the existing button-center hit test after fresh rediscovery.

If:

```text
pointer_hit.inside_post_button = True
```

the original transparent overlay may have disappeared.

In that case, do **not** immediately switch to keyboard.

Choose the safest existing behavior.

Preferred options:

```text
either fail the original mouse attempt without a second activation
```

or, only if the architecture can prove no click was delivered and explicitly permits one bounded re-attempt, carefully document that policy.

The safest narrow implementation is:

```text
keyboard fallback only while fresh hit-test still proves interception
```

---

# ============================================================

# PART D — SAFE KEYBOARD FOCUS

# ============================================================

# 13. FOCUS THE EXACT VERIFIED POST BUTTON

Use Playwright's normal focus mechanism on the freshly resolved Post button.

Conceptually:

```python
fresh_post_button.focus()
```

Do not click to focus it.

Do not Tab repeatedly from some unknown location.

---

# 14. VERIFY document.activeElement

After focusing, verify that browser focus genuinely belongs to:

```text
the Post button itself
or an element semantically inside that button, if browser behavior requires it
```

Prefer an explicit DOM relationship check.

Conceptually:

```javascript
const active = document.activeElement;
return button === active || button.contains(active);
```

Adapt this safely to Playwright locator/evaluate APIs.

Do not rely solely on:

```text
button looks focused
```

or CSS.

---

# 15. IF FOCUS VERIFICATION FAILS

Do not press Enter.

Return/log a stable failure such as:

```text
post_button_keyboard_focus_failed
```

or another naming convention consistent with the project.

Record diagnostics.

No second activation method should follow.

---

# ============================================================

# PART E — PRESS EXACTLY ONE KEY

# ============================================================

# 16. USE ENTER ONLY

After verified focus:

```text
press Enter exactly once
```

Do not do:

```text
Enter
then Space
```

Do not send Enter twice.

Do not combine:

```text
keyboard activation
+
force click
```

One fallback activation only.

An enabled semantic button should respond to normal Enter activation.

---

# 17. NO SPACE FALLBACK AFTER ENTER

If Enter does not produce verified success:

```text
fail safely
```

Do not then try:

```text
Space
mouse
JavaScript click
force=True
coordinates
```

because after Enter the outcome is ambiguous.

The central invariant is:

```text
at most one mouse activation attempt
+
at most one keyboard activation attempt,
and keyboard only after evidence the mouse was blocked
```

Even better, if your implementation can prove the normal Playwright click never dispatched because actionability was blocked before event delivery, document that proof.

---

# ============================================================

# PART F — SUCCESS CONFIRMATION AFTER KEYBOARD ACTIVATION

# ============================================================

# 18. EXPLICIT X SUCCESS REMAINS REQUIRED

After pressing Enter:

```text
wait for existing positive publication confirmation
```

Success still requires:

```text
Your post was sent
```

Do not infer success from:

```text
dialog disappearing
button disappearing
URL changing
video disappearing
Home page becoming visible
Enter returning without exception
```

---

# 19. USE A BOUNDED CONFIRMATION WINDOW

Reuse the existing post-success confirmation semantics where appropriate.

Do not accidentally give keyboard activation another 180-second budget.

Use an application-controlled bounded result-confirmation window consistent with the current publisher architecture.

---

# 20. GENERIC ERROR + SUCCESS

Preserve:

```text
SUCCESS + ERROR → posted
ERROR only      → failure
SUCCESS only    → posted
neither         → existing bounded timeout/unverified behavior
```

---

# ============================================================

# PART G — FAILURE REASONS

# ============================================================

# 21. DISTINGUISH MOUSE FAILURE FROM KEYBOARD FALLBACK FAILURE

Use stable reasons consistent with project conventions.

Possible examples:

```text
post_button_click_timeout
post_button_pointer_intercepted
post_button_keyboard_focus_failed
post_button_keyboard_activation_unverified
```

Do not proliferate reasons unnecessarily if existing generic reasons are sufficient.

The logs must still make the phase obvious.

---

# 22. TRUE KEYBOARD FAILURE MUST NOT BECOME SUCCESS

If:

```text
Enter sent
but no "Your post was sent"
```

do not claim publication.

Return:

```text
unverified
```

or another existing safe ambiguous-result reason if appropriate.

After keyboard activation, do not retry.

An operator may need to inspect X manually before another attempt.

---

# ============================================================

# PART H — LOGGING AND DIAGNOSTICS

# ============================================================

# 23. LOG WHEN FALLBACK IS BEING USED

Add one concise warning/info line:

```text
X Post mouse click was blocked by verified pointer interception;
attempting one keyboard activation on the freshly validated Post button
```

Do not hide this event.

It is useful operational evidence.

---

# 24. RECORD WHY FALLBACK WAS ALLOWED

Diagnostic structure should include:

```text
mouse_click_timeout_seconds
playwright_error classification
pointer_intercepted = true
pointer_hit
fresh_compose_root
fresh_button_testid
fresh_button_visible
fresh_button_enabled
attachment_count
composer_non_empty
video_media_state
focus_verified
keyboard_key = Enter
```

Do not log full HTML.

---

# 25. PRESERVE FAILURE SCREENSHOTS

Keep existing screenshot capture.

Optionally capture another screenshot immediately before keyboard fallback if the existing diagnostic infrastructure supports it cheaply.

Do not make screenshot capture mandatory for successful fallback.

---

# ============================================================

# PART I — DEDUP AND DUPLICATE-POST SAFETY

# ============================================================

# 26. DATABASE SUCCESS STATE ONLY AFTER VERIFIED POST

Do not alter existing DB semantics.

Only:

```python
{"ok": True, "reason": "posted"}
```

based on explicit positive X confirmation may trigger:

```text
source_seen
successful hash
perceptual successful state
posts_ok
```

---

# 27. NO SUCCESS AFTER MOUSE TIMEOUT WITHOUT POSITIVE SIGNAL

If normal click times out and reconciliation sees no positive success:

```text
do not mark published
```

Keyboard fallback may then be considered only under the narrow pointer-interception rules.

---

# 28. NO AUTOMATIC RETRY AFTER KEYBOARD ACTIVATION

Once Enter has been sent:

```text
never activate Post again during that post() call
```

Even if confirmation times out.

This protects against duplicate posts.

---

# ============================================================

# REQUIRED TESTS — CLASSIFICATION

# ============================================================

# Test A — pointer interception recognized

Fake exception:

```text
Locator.click: Timeout 15000ms exceeded.
...
<div ...> intercepts pointer events
```

and:

```text
pointer_hit.inside_post_button = False
```

Expected:

```text
classification = pointer_intercepted
```

---

# Test B — generic timeout does NOT qualify

Fake:

```text
Locator.click: Timeout 15000ms exceeded.
element is not stable
```

Expected:

```text
no keyboard fallback
```

---

# Test C — detached element does NOT qualify

Expected:

```text
no keyboard fallback
```

---

# Test D — browser/context closed does NOT qualify

Expected:

```text
no keyboard fallback
```

Existing browser recovery policy remains separate.

---

# ============================================================

# REQUIRED TESTS — RECONCILE BEFORE FALLBACK

# ============================================================

# Test E — mouse timeout but X actually posted

Sequence:

```text
mouse click raises pointer-interception-like timeout
BUT
during existing reconciliation:
"Your post was sent" appears
```

Expected:

```text
result = posted
keyboard activation count = 0
```

Mandatory duplicate-safety regression.

---

# Test F — success + generic error during reconciliation

Expected:

```text
posted
keyboard activation count = 0
```

Preserve success precedence.

---

# ============================================================

# REQUIRED TESTS — FRESH VALIDATION

# ============================================================

# Test G — original button stale, fresh button valid

Sequence:

```text
mouse click timeout
old dialog/button becomes stale
new visible active compose exists
fresh Post enabled
pointer interception still proven
```

Expected keyboard fallback targets only the fresh button.

---

# Test H — active attachment disappeared

Expected:

```text
no keyboard activation
```

---

# Test I — caption disappeared

If caption was requested:

```text
no keyboard activation
```

---

# Test J — Post became disabled

Expected:

```text
no keyboard activation
```

---

# Test K — captcha/login appears before fallback

Expected:

```text
no keyboard activation
existing auth/challenge failure
```

---

# Test L — secondary media editor appears

Expected:

```text
no keyboard activation until existing media-editor safety contract resolves
```

---

# ============================================================

# REQUIRED TESTS — FOCUS

# ============================================================

# Test M — focus verified

Fresh Post button:

```text
visible=True
enabled=True
```

After focus:

```text
document.activeElement == button
```

Expected:

```text
Enter permitted
```

---

# Test N — activeElement is valid button descendant

If browser DOM behavior causes a legitimate descendant to receive focus and the relationship is semantically safe:

```text
button.contains(activeElement) == True
```

Expected behavior according to implementation contract.

Do not loosen this without evidence.

---

# Test O — focus lands elsewhere

Fake:

```text
activeElement = overlay/background element
```

Expected:

```text
Enter count = 0
result = keyboard focus failure
```

---

# ============================================================

# REQUIRED TESTS — KEYBOARD FALLBACK

# ============================================================

# Test P — exact production failure shape

Simulate:

```text
video Ready
correct active-compose Post button
visible=True
enabled=True
stable=True

normal click:
PlaywrightTimeoutError
"intercepts pointer events"

pointer hit:
inside_post_button=False

5s reconciliation:
no success

fresh validation:
all valid

focus:
verified

Enter:
activates Post

X:
"Your post was sent"
```

Expected:

```python
{"ok": True, "reason": "posted"}
```

And:

```text
mouse click calls = 1
Enter presses     = 1
force clicks      = 0
JS clicks         = 0
Space presses     = 0
```

This is the critical regression.

---

# Test Q — Enter sent but no positive success

Expected:

```text
NOT success
no second activation
```

Use existing `unverified` semantics if appropriate.

---

# Test R — Enter produces generic error only

Expected:

```text
failure
no second activation
```

---

# Test S — Enter success + generic error coexist

Expected:

```text
posted
```

Positive explicit success still wins.

---

# Test T — pointer hit now belongs to Post button

After reconciliation, simulate overlay disappeared:

```text
inside_post_button=True
```

Expected:

```text
do not use keyboard fallback under the pointer-interception-only policy
```

Do not silently invent another mouse retry unless explicitly justified and tested.

---

# ============================================================

# REQUIRED TESTS — NO DUPLICATE ACTIVATION

# ============================================================

# Test U — normal mouse click succeeds

Expected:

```text
mouse clicks = 1
keyboard activation = 0
```

---

# Test V — normal click timeout, reconciliation proves success

Expected:

```text
mouse clicks = 1
keyboard activation = 0
result = posted
```

---

# Test W — qualified pointer interception

Expected:

```text
mouse click attempts = 1
keyboard Enter       = at most 1
```

---

# Test X — keyboard outcome unverified

Expected:

```text
no mouse retry
no second Enter
no Space
```

---

# ============================================================

# REQUIRED TESTS — DEDUP

# ============================================================

# Test Y — verified keyboard success finalizes normally

Expected:

```text
posts_ok increments once
source_seen written once
success hash written once
perceptual success state written once
```

---

# Test Z — keyboard activation unverified does not finalize success state

Expected:

```text
failed/ambiguous attempt may be recorded

BUT

source_seen success state unchanged
hash success state unchanged
perceptual success state unchanged
```

---

# ============================================================

# INDEPENDENT REPRODUCTIONS

# ============================================================

Do not rely only on pytest.

---

# 29. REPRODUCTION 1 — EXACT OVERLAY FAILURE

Use the actual production helper/post flow with fake Playwright objects.

Reproduce:

```text
correct enabled button
→ mouse click times out
→ exception explicitly says pointer events intercepted
→ hit test outside button
→ no success during reconciliation
→ fresh state valid
→ focus succeeds
→ Enter
→ positive X success
```

Report:

```text
mouse_click_count
keyboard_enter_count
result
```

Expected:

```text
1
1
posted
```

---

# 30. REPRODUCTION 2 — AMBIGUOUS MOUSE ATTEMPT ALREADY SUCCEEDED

Simulate:

```text
mouse click raises timeout
→ success toast appears 2s into reconciliation
```

Expected:

```text
mouse_click_count = 1
keyboard_enter_count = 0
result = posted
```

This is mandatory.

---

# 31. REPRODUCTION 3 — NON-POINTER CLICK TIMEOUT

Simulate:

```text
element detached
```

Expected:

```text
keyboard_enter_count = 0
```

---

# 32. REPRODUCTION 4 — POINTER INTERCEPTION BUT FOCUS FAILS

Expected:

```text
Enter count = 0
safe failure
```

---

# 33. REPRODUCTION 5 — KEYBOARD SENT, NO SUCCESS

Expected:

```text
Enter count = 1
no second activation
result != posted
```

---

# ============================================================

# SELF-REVIEW

# ============================================================

Before completion, answer all of these explicitly:

```text
Is normal mouse click still the primary mechanism?

Can any generic click timeout trigger keyboard fallback?

Is pointer interception independently verified?

Does reconciliation happen BEFORE keyboard fallback?

If the mouse actually posted, can Enter still be sent accidentally?

Is the compose freshly resolved again after the mouse timeout?

Are attachment and caption revalidated?

Is Post still visible and enabled before Enter?

Is focus explicitly verified?

Can Enter be sent to the wrong focused element?

Can Enter be pressed more than once?

Is Space ever tried afterward?

Did I add force=True anywhere?

Did I add JavaScript element.click() anywhere?

Did I remove/hide X overlay DOM?

Can compose disappearance count as success?

Is "Your post was sent" still mandatory?

Can an unverified keyboard activation poison dedup?

Did I modify scheduler/media/scraper behavior?
```

If any answer is weak:

```text
fix
→ test
→ reproduce
→ review again
```

---

# ============================================================

# REGRESSION REQUIREMENTS

# ============================================================

Re-run existing tests for:

```text
active compose ownership
hidden/stale dialogs
Post-button ownership
video Ready detection
60/180 media readiness
15-second mouse click timeout
pointer-hit diagnostics
Playwright error logging
post-click success reconciliation
post-click success precedence
caption validation
attachment validation
browser recovery
publisher failure reasons
dedup finalization
daemon
scheduler
```

Do not weaken existing tests to accommodate the new fallback.

---

# 34. FULL VALIDATION

Run:

```bash
python -m pytest -q
```

Then:

```bash
python -m compileall .
```

Run safe offline checks where supported:

```text
main.py --selftest
main.py --dry-run --seed-demo
```

Do NOT perform a live X post during automated agent testing.

Do NOT require live X/network/browser access.

---

# ============================================================

# ACCEPTANCE CRITERIA

# ============================================================

Do not claim fixed unless all are true:

1. Normal Playwright mouse click remains primary.
2. Existing 15-second mouse click timeout remains.
3. Mouse timeout is reconciled for positive success before any fallback.
4. If reconciliation sees success, keyboard is never used.
5. Keyboard fallback is limited to proven pointer-interception failures.
6. Generic timeouts do not trigger keyboard fallback.
7. Detached/stale/context-closed failures do not trigger keyboard fallback.
8. Fresh active compose is rediscovered before fallback.
9. Fresh Post button belongs to that active compose.
10. Caption is revalidated where required.
11. Attachment is revalidated.
12. Video/media state remains valid.
13. Button is freshly visible and enabled.
14. Fresh hit test still proves pointer interception.
15. Button receives keyboard focus without a pointer click.
16. Focus ownership is explicitly verified.
17. Enter is pressed exactly once.
18. Space is not subsequently tried.
19. `force=True` is not introduced.
20. JavaScript synthetic click is not introduced.
21. Overlay DOM is not deleted/modified to bypass X.
22. No raw-coordinate click fallback is introduced.
23. Explicit `"Your post was sent"` remains mandatory.
24. Success + generic error still resolves to posted.
25. Keyboard fallback with no explicit success is not treated as success.
26. No automatic retry occurs after keyboard activation.
27. Verified keyboard success finalizes dedup exactly once.
28. Unverified keyboard activation does not create successful dedup state.
29. Independent exact-overlay reproduction passes.
30. Independent mouse-timeout-but-already-posted reproduction proves keyboard count = 0.
31. Focus-failure reproduction proves Enter count = 0.
32. Focused tests pass.
33. Full safe regression suite passes.
34. `compileall` passes.

---

# ============================================================

# FINAL REPORT FORMAT

# ============================================================

When finished, report:

## Goals

List the actual engineering goals.

## Confirmed production root cause

Explain the evidence:

```text
Post button visible/enabled/stable
BUT
transparent/non-semantic div intercepts pointer events
```

## Files changed

List production/test/config/docs files.

## Timeout classifier

Explain exactly how:

```text
pointer_intercepted
```

is distinguished from other click timeouts.

Do not claim CSS class names are stable identifiers.

## Mouse reconciliation ordering

Show:

```text
mouse timeout
→ reconcile positive success
→ only then consider keyboard
```

## Fallback eligibility

List every required precondition.

## Fresh validation

Explain how active compose, attachment, caption, video state, and Post button are rediscovered/revalidated.

## Focus verification

Show how the code proves the correct Post button owns keyboard focus before Enter.

## Keyboard activation

Show:

```text
Enter count = exactly 1
Space count = 0
force clicks = 0
JS clicks = 0
```

## Verified success proof

Show exact production/fake result:

```text
pointer interception
→ mouse timeout
→ no mouse success
→ fresh validation
→ focus
→ Enter
→ "Your post was sent"
→ posted
```

## Duplicate-prevention proof

Show:

```text
mouse timeout
→ success appears during reconciliation
→ Enter count = 0
```

## Failure proof

Show:

```text
Enter sent
→ no explicit X success
→ NOT posted
→ no second activation
```

## Dedup proof

Show success finalizes exactly once and ambiguous failure does not poison successful state.

## Focused tests

Give exact counts.

## Full validation

Give exact:

```text
pytest
compileall
selftest/dry-run if executed
```

## Remaining risks

Only genuine external X/Playwright risks.

Do not hide any unmet acceptance criterion.

---

# COMPLETION RULE

Do not stop until:

```text
proven pointer-interception classifier
+
mouse-result reconciliation BEFORE fallback
+
fresh active-compose/button revalidation
+
focus verification
+
exactly one Enter fallback
+
no force/JS/coordinate click bypass
+
explicit X success remains mandatory
+
no duplicate activation
+
dedup safety preserved
+
focused regressions pass
+
independent reproductions pass
+
full suite passes
```

If not:

```text
inspect
→ revise
→ test
→ reproduce
→ self-review
→ repeat
```

Keep this repair narrowly focused on the **transparent pointer-intercepting layer shown by the real Playwright diagnostics and failure screenshot**.
