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
- **The arithmetic is the ceiling.** `order_calculator.calculate_order` decides the most that can be
  ordered. The Claude agent in `ordering_agent.py` may order less (it sees order history), never more;
  `cap_to_calculated` enforces that in code. Nobody checks the order before the supplier gets it.
- **Model output is never trusted as stock.** `stock_parser._validate_parsed` rejects unknown items,
  units and out-of-range quantities before anything is saved.
- **One order per order week** (Wednesday to Tuesday). A second run in a week that is already handled
  is skipped unless forced. "Handled" includes a supplier SMS that raised: it is unknown whether it
  arrived, so the bot never re-sends that on its own.
- **The agent can trim an order, not cancel one.** If stock is short and the agent orders nothing,
  the calculated order is sent.
- The only numbers the bot may text are `SUPPLIER_PHONE_NUMBER` and `EMPLOYEE_PHONE_NUMBERS`.
- Users: café staff on their phones. No screen, no login. Anything they need to do must work by SMS.

## Correctness traps
| Trap | Test |
|---|---|
| The host runs in UTC, where 9am Wednesday in Melbourne is still Tuesday: `date.today()` gives the wrong order date and holiday window | `test_melbourne_today_is_the_cafes_date_not_the_hosts` |
| A restart, the manual trigger or a retry sends the supplier the same order twice | `test_second_run_in_the_same_week_does_not_reorder`, `test_order_sent_before_the_run_log_existed_still_blocks_a_resend`, `test_force_resends_but_not_on_a_double_click` |
| "Once a week" counted in days: an order released late on Friday silently cancels next Wednesday's | `test_order_released_late_does_not_cancel_next_wednesdays_order`, `test_order_week_runs_wednesday_to_tuesday` |
| The agent returns a wild quantity, or orders nothing while stock is short | `test_agent_cannot_order_more_than_the_arithmetic`, `test_agent_cannot_cancel_an_order_when_stock_is_short`, `test_malformed_order_is_sent_back_to_the_agent_not_accepted` |
| Any text from staff ("thanks") counts as a fresh stock report | `test_bad_parse_is_rejected_not_saved_as_stock`, `test_sms_from_an_unknown_number_or_unparseable_text_saves_nothing` |
| One item counted this week makes the others' old counts look fresh, or a missing item is read as zero stock and fully re-ordered | `test_one_fresh_item_does_not_hide_the_missing_ones`, `test_stale_items_flags_old_counts` |
| The job asks staff for a count, they send it, and no order ever goes out — or the counts that were fresh on Wednesday go stale while staff find the missing one | `test_late_count_by_sms_releases_the_waiting_order`, `test_counts_fresh_when_the_job_asked_still_release_the_order` |
| The supplier SMS raises and staff are told "nothing was sent" when it may have been (then both the bot's order and a manual one arrive) | `test_supplier_send_error_is_not_reported_as_nothing_sent_and_not_resent`, `test_failure_before_the_send_tells_staff_and_can_be_retried` |
| The process is restarting at 9:00 Wednesday and the week's run is dropped | `test_missed_run_is_detected_only_on_order_day_after_nine` |
| Bottle items (lactose free, coconut) shown as boxes in the staff summary | `test_confirmation_totals_use_the_unit_staff_count_in` |
| `/trigger` reachable with a key that is in the code | `test_trigger_is_off_without_a_configured_key` |

## How to run, test and deploy (routines)
- Run: `python app.py` (the scheduler only starts this way, not under gunicorn or `flask run`)
- Unit tests: `python -m pytest -q` — no SMS, no model calls, throwaway database
- `python dry_run.py` calls the real Claude API and writes a test row to the real `data/stock.db`
- `python trigger_order.py` and `/trigger?key=…` send **real SMS** to the supplier and staff
- Deploy: Railway (see Notion → Services & Accounts → Railway for how it is wired). Env vars are in
  Railway's Variables tab; `TRIGGER_KEY` must be set there or `/trigger` answers 403. Rollback: redeploy
  the previous deployment in Railway.
- Scheduled job: Wednesday 9:00am Australia/Melbourne, in-process (APScheduler). Every run is written
  to the `job_runs` table and to stdout (Railway logs).
- Before merging to main or going live: run the `harden` skill. Before handing over: `repo-handover`.

## Handover (for the next person, or the client)
- Accounts (Railway, Twilio, Anthropic, GitHub `konlenka/supplier.agent`) are in Christian's name;
  status lives in Notion → Crème Handover.
- Where secrets live: `.env` locally, Railway Variables in production.
- What only works because of Christian's setup: the Railway start command and whether `data/` sits on a
  persistent volume are set in the Railway dashboard, not in this repo.
- The supplier's first name is written into `format_order_message`. A change of supplier or rep is a
  code change, not a setting.
