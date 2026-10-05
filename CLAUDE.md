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
- The only numbers the bot may text are `SUPPLIER_PHONE_NUMBER` and `EMPLOYEE_PHONE_NUMBERS`.
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

## How to run, test and deploy (routines)
- Run: `python app.py` (the scheduler only starts this way, not under gunicorn or `flask run`).
  `Procfile` carries the same command for Railway; a start command set in the Railway dashboard
  overrides it, so keep the two the same.
- Unit tests: `python -m pytest -q` — no SMS, no model calls, throwaway database
- `python dry_run.py` calls the real Claude API and writes a test row to the real `data/stock.db`
- `python trigger_order.py` and `/trigger?key=…` send **real SMS** to the supplier and staff
- Deploy: Railway (see Notion → Services & Accounts → Railway for how it is wired). Env vars are in
  Railway's Variables tab; `TRIGGER_KEY` must be set there or `/trigger` answers 403. Rollback: redeploy
  the previous deployment in Railway.
- Scheduled jobs, in-process (APScheduler), Australia/Melbourne: the order run Wednesday 9:00am. When
  a run has to ask for a count it books two follow-ups timed from that request: a reminder 4 hours
  later and the closing text 24 hours later (1:00pm Wednesday and 9:00am Thursday for the normal
  run). They live in memory, so startup re-books them for a request that is still open; a restart
  across the 4-hour mark drops the reminder, never the closing text. Every run and follow-up is
  written to the `job_runs` table and to stdout (Railway logs).
- Before merging to main or going live: run the `schneier` skill. Before handing over: `hightower`.

## Handover (for the next person, or the client)
- Accounts (Railway, Twilio, Anthropic, GitHub `konlenka/supplier.agent`) are in Christian's name;
  status lives in Notion → Crème Handover.
- Where secrets live: `.env` locally, Railway Variables in production.
- What only works because of Christian's setup: whether `data/` sits on a persistent volume, the port
  (`app.py` listens on 5000) and any start-command override are set in the Railway dashboard, not in
  this repo. Without the volume a redeploy wipes stock, order history and the once-a-week guard.
- The supplier's first name is written into `format_order_message`. A change of supplier or rep is a
  code change, not a setting.
