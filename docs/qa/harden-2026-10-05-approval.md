# Harden pass — approval step — Crème supplier bot — 5 Oct 2026

Scope: the approval-step diff only, `64e267b..bfb0700` on branch `feature/owner-approval`. The whole
repo was hardened earlier the same day (`harden-2026-10-05.md`); this pass does not repeat it.
**Committed locally, not pushed, not merged, not deployed.** Production (Railway) is untouched.

Result: `python -m pytest -q` — 264 passed (73 before the approval work). No SMS or model call is made
by the suite. Mutation check: 95 deliberate breakages of the approval path, each caught by a test,
plus 3 for the last reader change.
Not done: no run against the real Twilio API (the account in `.env` owned no phone number on 5 Oct),
no real-case run, no look at Railway.

## Hygiene

| Check | Result |
|---|---|
| Uncommitted changes | None |
| Unpushed commits | **6 on `feature/owner-approval`, 1 of them also on `harden/2026-10-05`. They exist only on this laptop.** `main` matches `origin/main` |
| Secrets in tracked files | None. `.env` is gitignored |
| Client data in git | None. `data/` is gitignored; the tests use made-up numbers |
| Tests | 264 passing |
| Pre-commit hook | Installed; ran on every commit |
| Dead code in the diff | None found (unused-name check over the five changed modules) |

Pushing is not automatic here: the earlier pass found that `konlenka/supplier.agent` is public. Pushing
these branches publishes them. Make the repo private first, or decide that is fine.

## Findings, ranked

| # | What goes wrong for the café | Status |
|---|---|---|
| 1 | **A reply that was not a plain yes sent the order.** "Ok I'll check the fridge first", "yes make the oat 4", "ok?", "ok 🤔", "ok нет" and "ok . . ." each reached the supplier at some point in the build. | **Fixed.** A yes must be the whole message: a yes-word, then only listed phrases, written only with letters, spaces, `.` `,` `!` and a yes emoji. Found by the `qa` agent over three reviews; the replies it reported are now in the tests. |
| 2 | **The approval step has never sent or received a real text.** Everything above is proven with fake SMS. | **Open. Not a code fix.** The Twilio account needs a number, then the real run: request arrives on a phone, a yes sends the order. |
| 3 | **A redeploy can lose an order that is waiting for approval, with nobody told**, if `data/` is not on a persistent Railway volume. The waiting order, its follow-ups and the record of the week all live in `data/stock.db`. | **Open. Not a code fix.** Same root as finding 2 of the earlier pass; the volume is still unchecked. |
| 4 | **Removing the approver's number while an order waited left it stranded**, and a follow-up could later tell staff nothing was sent after a forced run had sent it. | **Fixed.** Cancelled at startup, staff told to order by hand. Remove the variable when nothing is waiting. |
| 5 | **A restart at the wrong second lost an answer.** Between the yes and the send being recorded, or between the no and staff being asked to recount, nobody knew where the order was. | **Fixed.** Startup reports the first as "may not have reached the supplier" (never re-sent) and re-asks for the second. |
| 6 | **The approver was promised a new order and then heard nothing** when the recount never came, needed no order, or the request reached no staff. | **Fixed.** They are texted in each case. |
| 7 | **If the approver replies STOP or CANCEL, Twilio may unsubscribe their number.** Every later approval request would then fail and each week would end in "order by hand". | **Open. Unverified.** The README tells the approver not to. Check Twilio's behaviour during the real run. |
| 8 | **Every no starts another 24 hours for the recount and another 24 for the answer.** With no limit on recounts (Christian's call), an order can go out days after Wednesday. | **Open. A question for the café:** what is the supplier's cut-off? If there is one, the week should close at a fixed time. |

## Later

- The yes path sends the supplier text and the staff texts inside Twilio's webhook request. Twilio waits 15 seconds; not measured.
- `sms.py` logs every recipient's phone number to stdout (Railway logs). Older than this diff; the approver's number now appears there too.
- A no followed by a change of mind in the same text ("No that's fine, send it") is a no and costs a recount.
- A reply split over two texts ("ok", then "but make the oat 4") sends on the first.
- While an order waits, a count texted by an approver who also counts stock is not saved.
- The two reader changes in `bfb0700` were made after the `qa` agent's "merge" verdict on `f97e842` and were not re-reviewed.

## Journeys

All at unit level, with fake SMS. Command: `python -m pytest -q` → `264 passed`.

| Journey | Result | Evidence |
|---|---|---|
| Count in → order held → approver says yes → supplier gets that order once | PASS | `test_yes_sends_the_supplier_the_order_the_boss_was_shown`, `test_a_second_yes_does_not_send_a_second_order`, `test_simultaneous_yeses_send_one_order` |
| Approver says no → staff recount → new order back to the approver → yes sends it | PASS | `test_a_recount_after_a_no_goes_back_to_the_boss_and_their_yes_sends_it`, `test_the_count_the_boss_turned_down_is_not_used_again` |
| Approver says nothing → one reminder → week closed, everyone told | PASS | `test_a_silent_boss_gets_one_reminder_with_the_order_in_it`, `test_no_answer_in_a_day_closes_the_week_and_tells_the_boss_and_staff` |
| A reply that is not a plain yes or no sends nothing | PASS | `test_replies_that_are_neither_are_not_guessed` (77 replies), `test_no_model_reads_the_bosses_reply` |
| Process restarts part-way through any of the above | PASS | the five `test_startup_…` tests |
| Approval switched off → the bot behaves as it did before | PASS | the 73 earlier tests, unchanged, plus `test_with_no_approver_set_nothing_is_held` |
| A real text to a real phone and back | **NOT RUN** | Twilio account has no number |
