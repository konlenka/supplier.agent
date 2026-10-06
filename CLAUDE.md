# CLAUDE.md — Crème Café supplier bot

Standing instructions for this folder. Read before making any change.

## What this is
Staff text their milk stock count to a Twilio number. Every Wednesday at 9:00am Melbourne time the
bot works out what is short of the targets in `config.py`, texts the order to the supplier, and texts
staff what was ordered. It replaces someone counting, working out the order and texting the supplier
by hand each week.

## Role
You are the developer on this build. Christian leads and approves. Follow
`Desktop/thelo/delivery/build-protocol.md` (branches, test tiers, the review gate). Every change here
touches something that sends an SMS, so the review gate (§6) applies to every merge.

## Context: where truth lives
- Business status: Notion → Clients & Projects → Engagements → Supplier bot
- Handover list: Notion → Crème Handover
- Client data in this folder: `data/stock.db` (stock reports with staff phone numbers, order history,
  run log) and `.env` (staff and supplier numbers). Both gitignored.

## Non-negotiable constraints (guardrails)
- **The order is arithmetic, not a model's decision.** `order_calculator.calculate_order` sets every
  quantity: target minus counted stock, in whole boxes. Nobody checks the order before the supplier
  gets it, so no model call belongs in the order job. (An agent used to sit here; by 2 Oct it could
  only make an order smaller, and it was removed on 5 Oct.) The one model call left reads staff texts.
- **Model output is never trusted as stock.** `stock_parser._validate_parsed` rejects unknown items,
  units and out-of-range quantities before anything is saved.
- **One order per order week** (Wednesday to Tuesday). A second run in a week that is already handled
  is skipped unless forced. "Handled" includes a supplier SMS that raised: it is unknown whether it
  arrived, so the bot never re-sends that on its own.
- **Never two orders for one shortfall.** A count can't include a delivery that hasn't arrived.
  `DELIVERY_DAYS` in `config.py` (2 — a thelo assumption, not confirmed with the café) is how long a
  delivery takes. Until then a run places no order (`recent_order`); a count taken before then is
  stale. Only a forced run is exempt, because it is a deliberate re-send.
- **A request for a count ends.** It stays open for `WAIT_FOR_COUNT_HOURS` (24). Then the closing
  follow-up ends the week (`closed_no_count`) and tells staff to order by hand, and a count only
  records stock. Without the end, a text days later released an order on top of the one staff had
  placed by hand. Every text that states a deadline must state the real one.
- **On trial, the approver's yes is the only thing that sends an order.** While
  `APPROVER_PHONE_NUMBER` is set, a run holds the order (`awaiting_approval`) and texts it to the
  approver with the counts behind it. A yes sends the supplier the order they were shown, not one
  re-worked from later counts. The reply is read by a fixed word list (`_read_approval_reply`),
  never a model, and anything unclear is asked again. Remove the variable and orders go straight out.
- **A no means count again, every time.** Staff are asked for a recount and only a count taken
  after the no can be ordered from (`_usable_from`); the new order goes back to the approver. There
  is no limit on the number of recounts (Christian's call, 5 Oct). The approver's words are kept in
  `order_approvals.reply`.
- **A wait for the approver ends.** One reminder after 4 hours; after `WAIT_FOR_APPROVAL_HOURS` (24)
  the week closes (`closed_not_approved`), nothing is sent, and the approver and staff are told to
  order by hand. A yes after that sends nothing.
- The only numbers the bot may text are `SUPPLIER_PHONE_NUMBER`, `EMPLOYEE_PHONE_NUMBERS` and
  `APPROVER_PHONE_NUMBER`.
- Users: café staff on their phones. No screen, no login. Anything they need to do must work by SMS.

## Correctness traps
| Trap | Test |
|---|---|
| The host runs in UTC, where 9am Wednesday in Melbourne is still Tuesday: `date.today()` gives the wrong order date and holiday window | `test_melbourne_today_is_the_cafes_date_not_the_hosts` |
| A restart, the manual trigger or a retry sends the supplier the same order twice | `test_second_run_in_the_same_week_does_not_reorder`, `test_order_sent_before_the_run_log_existed_still_blocks_a_resend`, `test_force_resends_but_not_on_a_double_click` |
| "Once a week" counted in days: an order released late on Friday silently cancels next Wednesday's | `test_order_released_late_does_not_cancel_next_wednesdays_order`, `test_order_week_runs_wednesday_to_tuesday` |
| A model is put back in charge of a quantity nobody checks | `test_order_job_makes_no_model_call` |
| An order went out a day or two ago and the next run orders the same shortfall again — from the old count, or from any text staff send before the delivery arrives | `test_no_order_while_the_last_delivery_is_still_on_its_way`, `test_count_from_before_the_delivery_was_due_is_asked_for_again`, `test_count_must_be_taken_once_the_delivery_is_due`, `test_unconfirmed_send_also_holds_the_next_order_and_is_described_honestly` |
| A forced run is blocked by the delivery rule (it is the deliberate re-send), or a forced run with no fresh count reopens a handled week as "waiting" and staff are told the order hasn't gone | `test_forced_run_may_reuse_the_counts_the_last_order_was_made_from`, `test_forced_run_without_a_fresh_count_does_not_reopen_a_handled_week` |
| The 9:00 request for a count is missed and the week ends with no order and nobody told | `test_missing_count_gets_one_reminder_and_the_count_still_releases_the_order`, `test_no_reminder_when_nothing_is_waiting`, `test_a_request_for_a_count_books_its_reminder_and_its_closing_text`, `test_a_restart_rebooks_the_follow_ups_for_an_open_request`, `test_a_follow_up_that_reaches_nobody_says_so_in_the_run_log` |
| Staff are told to order by hand, do, and a count texted later that week places a second order | `test_no_count_by_thursday_closes_the_week_and_a_late_count_only_records_stock`, `test_the_wait_for_a_count_ends_after_a_day_even_if_the_closing_text_never_ran`, `test_a_late_reminder_does_not_nag_once_the_wait_is_over` |
| The count lands in the seconds while the run is asking for it (or the thread releasing the order dies in a restart): the count is complete, nothing is missing to chase, and no order ever goes | `test_count_that_arrives_while_the_run_is_asking_for_it_still_gets_ordered`, `test_reminder_places_the_order_if_the_count_is_complete_but_it_never_went`, `test_closing_always_ends_the_week_with_a_text_even_if_the_count_is_complete` |
| The follow-ups judge counts by a different rule than the order run does | `test_follow_ups_use_the_delivery_rule_too` |
| A restart inside the wait books only the closing text, so a complete count is never ordered; or a closing job left from an earlier request closes a new one early | `test_a_restart_inside_the_wait_still_gets_a_complete_count_ordered`, `test_a_closing_job_left_over_from_an_earlier_request_does_not_close_a_new_one` |
| The weekly job is moved off Wednesday 9:00 Melbourne time, or a text states a deadline in the wrong timezone | `test_the_weekly_order_is_booked_for_wednesday_nine_melbourne_time`, `test_deadline_is_spoken_in_the_cafes_time` |
| A forced run with no count in a week with no order is refused silently | `test_forced_run_with_no_count_in_an_unhandled_week_still_asks_staff` |
| A new test passes for the wrong reason (a swallowed error, a helper that moves the wrong timestamp) | Mutation-check a change to the order path: break the line, confirm a test fails. The 5 Oct pass did this for 38 mutants. Not covered: nothing tests that the follow-ups run under the order lock, or the startup steps in `__main__` |
| Any text from staff ("thanks") counts as a fresh stock report | `test_bad_parse_is_rejected_not_saved_as_stock`, `test_sms_from_an_unknown_number_or_unparseable_text_saves_nothing` |
| One item counted this week makes the others' old counts look fresh, or a missing item is read as zero stock and fully re-ordered | `test_one_fresh_item_does_not_hide_the_missing_ones`, `test_stale_items_flags_old_counts` |
| The job asks staff for a count, they send it, and no order ever goes out — or the counts that were fresh on Wednesday go stale while staff find the missing one | `test_late_count_by_sms_releases_the_waiting_order`, `test_counts_fresh_when_the_job_asked_still_release_the_order` |
| The supplier SMS raises and staff are told "nothing was sent" when it may have been (then both the bot's order and a manual one arrive) | `test_supplier_send_error_is_not_reported_as_nothing_sent_and_not_resent`, `test_failure_before_the_send_tells_staff_and_can_be_retried` |
| The process is restarting at 9:00 Wednesday and the week's run is dropped | `test_missed_run_is_detected_only_on_order_day_after_nine` |
| Bottle items (lactose free, coconut) shown as boxes in the staff summary | `test_confirmation_totals_use_the_unit_staff_count_in` |
| `/trigger` reachable with a key that is in the code | `test_trigger_is_off_without_a_configured_key` |
| The host stops an idle machine and it sleeps through 9:00 Wednesday; the database is off the volume and a deploy wipes the week's record; the app is started in a way that never books the weekly job; traffic goes to the wrong port | `tests/test_deploy_config.py` |
| Two machines run at once (Fly can add a spare for redundancy): two bots, two databases, and the supplier gets every order twice | No test can see this. `fly deploy --ha=false`, then `fly scale show` |
| The machine crashes on start, or exits, and Fly's default restart policy leaves it stopped | `test_a_machine_that_exits_is_always_started_again` |
| `TWILIO_AUTH_TOKEN` is missing on the host and a request signed with an empty key is accepted; or the host's proxy does not say the request came by https and every staff text is refused | `test_with_no_auth_token_set_every_incoming_text_is_refused`, `test_a_twilio_request_arriving_through_the_hosts_proxy_is_accepted`, `test_the_signature_only_matches_if_the_proxy_says_the_request_came_by_https`. Whether Fly's proxy sends what the check needs is only proven by the first real text |
| Staff or supplier phone numbers written to logs that leave the machine | `test_phone_numbers_are_not_written_to_the_logs`, `test_the_sms_module_logs_who_it_texted_without_the_number` |
| A rebuild months later pulls a dependency version nobody tested (APScheduler 4 has a different API) | `test_every_dependency_is_pinned`; the image build runs the suite |
| With approval on, an order reaches the supplier without a yes: straight from the run, from a re-run or forced run while one is waiting, or from a count that completes the wait | `test_with_approval_on_the_order_goes_to_the_boss_and_not_the_supplier`, `test_another_run_while_waiting_for_the_boss_asks_and_orders_nothing`, `test_a_count_that_completes_the_wait_goes_to_the_boss_and_staff_are_told_so` |
| A yes releases something other than what the approver saw (re-worked from a later count), or releases it twice | `test_yes_sends_the_supplier_the_order_the_boss_was_shown`, `test_a_second_yes_does_not_send_a_second_order` |
| A yes from the wrong person, a day late, or for a request that never reached the approver sends an order | `test_a_yes_from_a_staff_number_is_not_an_approval`, `test_a_yes_after_the_wait_is_over_sends_nothing_and_closes_the_week`, `test_a_request_that_never_reached_the_boss_cannot_be_approved`, `test_next_weeks_request_replaces_one_the_boss_never_answered` |
| A reply that is not a plain yes is taken as one. "Ok" is also how people acknowledge a text: "ok I'll check the fridge first", "yes make the oat 4", "ok?", "ok, is that all", "ok 🤔" and "ok нет" each sent the order at some point in the 5 Oct reviews. A yes is the whole message: a yes-word followed only by phrases from `_YES_TAILS`, written only with the characters `_NOT_PART_OF_A_YES` allows. Add to those lists with care, never as loose words, and never widen the allowed characters. On the no side, "no worries" and "not sure" must not send staff back to count (`_NOT_A_NO`) | `test_replies_read_as_yes`, `test_replies_read_as_no`, `test_replies_that_are_neither_are_not_guessed`, `test_an_unclear_reply_sends_nothing_and_the_order_stays_open`, `test_no_model_reads_the_bosses_reply` |
| Two requests act on the same waiting order (two yeses at once, a yes racing the closing job) and it is sent twice. The claim in `storage.decide_approval` is what stops it; the order lock alone is not tested | `test_two_requests_acting_on_the_same_waiting_order_send_it_once`, `test_a_no_or_a_closing_text_acting_on_an_order_already_dealt_with_does_nothing`, `test_simultaneous_yeses_send_one_order` |
| The approver is told "you'll get the new order to approve" and then hears nothing: the recount never came, it needed no order, or the request to recount reached no staff | `test_no_recount_within_a_day_closes_the_week`, `test_a_recount_that_needs_no_order_tells_the_boss_too`, `test_a_no_that_reaches_no_staff_tells_the_boss_so` |
| Approval is switched off (variable removed) while an order is waiting: it sits "waiting" for ever, and a follow-up later tells staff nothing was sent after a forced run sent it | `test_switching_approval_off_while_an_order_waits_cancels_it_and_tells_staff`, `test_an_order_still_waiting_is_untouched_at_startup_while_approval_is_on`, `test_no_reminder_is_attempted_with_no_approver_number` |
| A restart lands between the approver's no and staff being asked to recount: the week is open with no request, no follow-ups and nobody told | `test_a_no_cut_off_before_staff_were_asked_is_picked_up_after_a_restart`, `test_a_no_that_was_fully_handled_is_left_alone_after_a_restart` |
| A recovery step is dropped from `_resume_after_restart`, or the orphaned order has follow-ups booked before it is cancelled | The five `test_startup_…` tests |
| A closing text promises "the next automatic order is next Wednesday" when an order asked late in the week closes after that Wednesday has started | `test_closing_texts_do_not_promise_when_the_next_automatic_order_is` |
| An approver who also counts stock sends a count that starts with "no" or "ok" and it is read as their answer | `test_a_boss_who_also_counts_stock_has_a_recount_starting_with_no_saved_as_a_count`, `test_a_count_texted_by_the_boss_while_an_order_waits_does_not_approve_it` |
| `APPROVER_PHONE_NUMBER` typed with spaces never matches the sender, so no yes is ever recognised | `test_the_approver_number_is_matched_however_it_was_typed` |
| A no sends the order anyway, loses the approver's reason, or leaves staff with nothing to do | `test_a_no_sends_nothing_keeps_the_reason_and_asks_staff_to_count_again`, `test_a_no_books_the_reminder_and_closing_text_for_the_recount` |
| The count the approver said no to is ordered from again: by a re-run, a forced run, the 4-hour reminder, or a part recount | `test_the_count_the_boss_turned_down_is_not_used_again`, `test_the_reminder_after_a_no_chases_the_recount_and_does_not_release_the_old_count`, `test_a_recount_after_a_no_goes_back_to_the_boss_and_their_yes_sends_it` |
| After a no the week stays "handled", so the recount never produces an order — or a second no ends the recounts | `test_a_recount_after_a_no_goes_back_to_the_boss_and_their_yes_sends_it`, `test_a_second_no_asks_for_another_recount`, `test_no_recount_within_a_day_closes_the_week` |
| Staff are told the order "goes out" once the count is in, when it goes to the approver first | `test_staff_are_told_the_order_goes_for_approval_not_straight_out`, `test_without_approval_staff_are_told_the_order_goes_out` |
| The approver never answers and the week ends with no order and nobody told; or they are reminded twice, nagged after the deadline, or a new request is closed by an old closing job | `test_asking_the_boss_books_a_reminder_and_a_closing_text`, `test_a_silent_boss_gets_one_reminder_with_the_order_in_it`, `test_no_answer_in_a_day_closes_the_week_and_tells_the_boss_and_staff`, `test_a_reminder_that_runs_after_the_deadline_does_not_nag`, `test_a_closing_job_left_from_an_earlier_request_does_not_close_a_new_one`, `test_the_boss_is_not_chased_once_they_have_answered` |
| A restart drops the approver's reminder and closing text, or lands between their yes and the send being recorded and nobody knows whether the supplier has the order | `test_a_restart_rebooks_the_follow_ups_for_an_order_still_with_the_boss`, `test_a_yes_cut_off_before_the_send_was_recorded_is_reported_not_assumed`, `test_a_yes_that_was_sent_and_recorded_is_left_alone_after_a_restart` |
| An unanswered order asked late in the week closes the following Wednesday's week | `test_an_order_asked_late_in_the_week_and_never_answered_does_not_cancel_next_wednesdays` |
| The supplier send errors after a yes and the approver and staff are told it was sent, or it is sent again | `test_supplier_send_error_after_a_yes_is_unconfirmed_and_not_resent` |
| An approver who also counts stock has their count swallowed as an unclear answer | `test_a_boss_who_also_counts_stock_can_do_both` |

## How to run, test and deploy (routines)
- Run: `python app.py` (the scheduler only starts this way, not under gunicorn or `flask run`).
  The `Dockerfile`'s `CMD` is that command. `Procfile` is left over from Railway and unused on Fly.
- Unit tests: `python -m pytest -q` — no SMS, no model calls, throwaway database
- `python dry_run.py` calls the real Claude API and writes a test row to the real `data/stock.db`
- `python trigger_order.py` and `/trigger` send **real SMS** to the supplier and staff. Send the
  key as the `X-Trigger-Key` header; as `?key=` it is written to the logs with the request
- Deploy: Fly.io, Sydney (`fly.toml`, `Dockerfile`; steps in README → Deployment). Always
  `fly deploy --ha=false`, then `fly scale show` must say one machine. The image build runs the suite
  and stops the deploy if it fails. Secrets are set with `fly secrets set`; `TRIGGER_KEY` must be set
  or `/trigger` answers 403. Rollback: `fly releases --image`, then
  `fly deploy --ha=false --image <the previous image>`. Off switch: `fly scale count 0 --yes`; back
  on with `fly deploy --ha=false`, then check one machine and the same one volume. Not rehearsed yet.
  **Not yet deployed there (6 Oct 2026):** the files are written and tested locally on Python 3.13
  with the pinned versions, but no image has been built and nothing has run on Fly. Railway was
  switched off when its trial ended on 4 May 2026.
- A count texted to a deployed bot is real stock, and a trigger run is a real order: there is no
  test mode. For the first deploy the supplier and staff numbers are set to Christian's mobile, the
  tests are run on a day other than Wednesday, and the database is deleted before the real numbers
  go in (README → First deploy, steps 10 to 12). After go-live the database is never deleted.
- Deploy-config mutation check, 6 Oct: 33 deliberate breakages of `fly.toml`, the `Dockerfile`,
  `.dockerignore` and the lines of code they depend on, each caught by `tests/test_deploy_config.py`.
- The packages the code imports are pinned in `requirements.txt` to the versions the suite passes
  with. Raise a pin on purpose, run the suite, then deploy; don't loosen them back to ranges. What
  those packages pull in, and the `python:3.13-slim` base image, are not pinned: the suite running
  inside the image build is the guard for them.
- Logs go to Fly's log pipeline, and where that keeps them is not documented, so phone numbers are
  logged as their last three digits (`sms.mask_phone`). Not covered: an error message from Twilio
  can quote a number, and the order itself (no personal data) is logged in full.
- Open before the café relies on it (build protocol §8): nobody is alerted if the machine is down
  or a run fails before it can text staff, beyond "no text by 9:15 means order by hand"; and the only
  off switch is a `fly` command, which the café cannot run. Fly.io is not yet in thelo's tool matrix.
- Scheduled jobs, in-process (APScheduler), Australia/Melbourne: the order run Wednesday 9:00am. When
  a run has to ask for a count it books two follow-ups timed from that request: a reminder 4 hours
  later and the closing text 24 hours later (1:00pm Wednesday and 9:00am Thursday for the normal
  run). They live in memory, so startup re-books them for a request that is still open; a restart
  across the 4-hour mark drops the reminder, never the closing text. Every run and follow-up is
  written to the `job_runs` table and to stdout (`fly logs`). An order sent for approval books
  the same pair for the approver (`chase_approval`), timed from when they were asked and re-booked
  at startup the same way. Every approval request, its answer and the approver's words are in the
  `order_approvals` table.
- Approval mutation check, 5 Oct: 95 deliberate breakages of the approval path, each caught by a
  test. Not covered: that `__main__` calls `_resume_after_restart`, the order lock around the
  approval paths (the claim in `decide_approval` is what the tests prove), whether the yes path's
  sends fit inside Twilio's 15-second webhook limit, how Twilio treats STOP/CANCEL from the
  approver, and a real SMS round trip (the Twilio account had no number on 5 Oct).
- Known and left:
  - While an order is waiting, a text from an approver who also counts stock is read as their
    answer, never as a count: one starting with "no" asks staff for a recount, anything else gets
    "Reply YES or NO". The count itself is not saved.
  - If the run-log write fails after a no (`_log_run` swallows the error), the week sits open with
    no request until the next restart, when `_recover_interrupted_no` asks.
  - An order asked on a Tuesday and approved before 9:00 on Wednesday makes that Wednesday's run a
    silent `skipped`, not `recent_order` with a text.
  - The closing text for a count that never came still says "the next automatic order is next
    Wednesday" (it predates the approval work and is true for the normal Wednesday run).
  - A split reply ("ok", then "but make the oat 4" as a second text) sends the order on the first.
  - A no-word followed by something that takes it back ("No that's fine, send it", "No sorry,
    yes") is still a no and asks staff for a recount. It never sends an order.
  - Some plain yeses are asked again rather than taken: "Yes :)", "Yes 😊", "Alright", "Correct".
  - Three reviews by the `qa` agent on 5 Oct ended in "merge" at `f97e842`. The two small reader
    changes after it (spaced-out dots, more `_NOT_A_NO` idioms) were not re-reviewed.
- Before merging to main or going live: run the `schneier` skill. Before handing over: `hightower`.

## Handover (for the next person, or the client)
- Accounts (Fly.io, Twilio, Anthropic, GitHub `konlenka/supplier.agent`) are in Christian's name, at
  the owner's request (6 Oct 2026: the owner does not want to open accounts). The café can have its
  data and its phone number on request. Status lives in Notion → Crème Handover.
- Where secrets live: `.env` locally, `fly secrets` in production.
- The volume, the port (5000), the region and the start command are all in this repo now
  (`fly.toml`, `Dockerfile`). What is not in the repo: the number of machines. That is set by how
  the deploy command is run, so check it after every deploy.
- The supplier's first name is written into `format_order_message`. A change of supplier or rep is a
  code change, not a setting.
