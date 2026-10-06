# Creme Cafe — Automated Milk Ordering System

Automated inventory and ordering system for Creme Cafe (70-72 Bay Street, Melbourne). Employees text in stock levels via SMS. Every Wednesday at 9am, the system automatically calculates and sends a milk order to the supplier.

---

## How it works

```
Employee texts stock levels (SMS)
        ↓
Claude AI parses the message → saves to local database
        ↓
Wednesday 9:00 AM Melbourne time (automatic)
        ↓
Order calculated: target minus counted stock, plus the seasonal uplift
        ↓
Texts the order to supplier
        ↓
Texts a confirmation summary to all employees
```

No human input required on ordering day — it runs fully automatically. The quantities are plain arithmetic from the targets in `config.py`; the AI only reads the staff texts, it does not decide what is ordered.

**If no text has arrived from the system by 9:15 on a Wednesday, place the order by hand.** The Wednesday 9:00 run normally ends in a text to staff (the order summary, "no order needed", a request for a count, or "skipped, an order went out recently"), so silence means it did not run.

---

## Stack

| Component | Technology |
|-----------|-----------|
| Web server / SMS webhook | Flask |
| SMS (inbound + outbound) | Twilio |
| Reading staff texts | Anthropic Claude (Haiku) |
| Order quantities | Arithmetic in `order_calculator.py` (no AI) |
| Database | SQLite |
| Scheduling | APScheduler |

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/konlenka/supplier.agent.git
cd supplier.agent
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Fill in the values:

```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_PHONE_NUMBER=+61xxxxxxxxx
SUPPLIER_PHONE_NUMBER=+61xxxxxxxxx
EMPLOYEE_PHONE_NUMBERS=+61xxxxxxxxx,+61xxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
TRIGGER_KEY=a-long-random-string
```

| Variable | Where to get it |
|----------|----------------|
| `TRIGGER_KEY` | Make one up (long and random). Unlocks the manual `/trigger` URL; leave empty to switch that URL off |
| `TWILIO_ACCOUNT_SID` | [Twilio Console](https://console.twilio.com) → Account Info |
| `TWILIO_AUTH_TOKEN` | Twilio Console → Account Info |
| `TWILIO_PHONE_NUMBER` | The Australian number you buy in Twilio |
| `SUPPLIER_PHONE_NUMBER` | Your milk supplier's mobile number |
| `EMPLOYEE_PHONE_NUMBERS` | Comma-separated staff numbers who report stock |
| `APPROVER_PHONE_NUMBER` | Optional. The person who must reply YES before an order goes to the supplier (see "Approval during the trial"). International format, `+614…`, not `04…`: a number written any other way is never recognised as the approver. Leave it out to send orders with no approval |
| `ANTHROPIC_API_KEY` | [Anthropic Console](https://console.anthropic.com) → API Keys |

> **Twilio trial accounts:** You must verify every number you send to (including the supplier) before messages will go through. Once you upgrade to a paid account, this restriction is removed.

### 4. Run locally

```bash
python app.py
```

The server starts on `http://localhost:5000`.

### 5. Expose your server to Twilio (local dev)

Twilio needs a public URL to forward incoming SMS to your server. Use [ngrok](https://ngrok.com):

```bash
ngrok http 5000
```

Copy the `https://xxxx.ngrok.io` URL. In your Twilio phone number settings, set:
- **"A message comes in"** → `https://xxxx.ngrok.io/sms` (POST)

---

## Deployment (Fly.io, Sydney)

The bot runs as one always-on container on [Fly.io](https://fly.io) in the Sydney region, with its database on a persistent volume. The setup is in `Dockerfile` and `fly.toml`. (It ran on Railway until May 2026; Railway has no Australian region.)

Three things must stay true, and `tests/test_deploy_config.py` checks the ones a file can show:

- **Exactly one machine.** The weekly job runs inside the app. Two machines are two bots and the supplier gets every order twice. Fly can add a spare machine for redundancy, so every deploy uses `--ha=false` and is followed by a check.
- **Never stopped.** A machine that is asleep, or that crashed and was not restarted, sleeps through 9:00 on Wednesday.
- **`data/` on the volume.** Off it, every deploy wipes the stock counts, the order history and the record of this week's order.

### First deploy

Run these in PowerShell, in this folder.

1. Install the Fly command line, then close and reopen the terminal:
   ```
   iwr https://fly.io/install.ps1 -useb | iex
   ```
2. Create the account and add a card in the dashboard (`fly auth login` if the account already exists):
   ```
   fly auth signup
   ```
3. Create the app. If the name is taken, pick another and change `app` in `fly.toml` to match:
   ```
   fly apps create creme-supplier-bot
   ```
4. Create the volume for the database:
   ```
   fly volumes create creme_data --region syd --size 1 --yes
   ```
5. Set the secrets. Same names as `.env.example`; real values, phone numbers in `+614…` format. Leave `APPROVER_PHONE_NUMBER` out to run with no approval step. **For the first deploy, set `SUPPLIER_PHONE_NUMBER`, `EMPLOYEE_PHONE_NUMBERS` and `APPROVER_PHONE_NUMBER` to your own mobile**, so the tests in steps 10 and 11 text nobody else. Keep the single quotes: without them PowerShell cuts a value short at a `$` or `;`:
   ```
   fly secrets set 'TWILIO_ACCOUNT_SID=...' 'TWILIO_AUTH_TOKEN=...' 'TWILIO_PHONE_NUMBER=...' 'SUPPLIER_PHONE_NUMBER=...' 'EMPLOYEE_PHONE_NUMBERS=...,...' 'ANTHROPIC_API_KEY=...' 'TRIGGER_KEY=...' 'APPROVER_PHONE_NUMBER=...'
   ```
   Then `fly secrets list` should show all eight names (seven without the approver). With `TWILIO_AUTH_TOKEN` missing the bot refuses every incoming text.
6. Deploy. The build runs the test suite inside the image and stops if it fails:
   ```
   fly deploy --ha=false
   ```
7. Check there is exactly one machine, and one volume attached to it. If it shows two machines, run `fly scale count 1`:
   ```
   fly scale show
   fly volumes list
   ```
8. In Twilio, set the number's **"A message comes in"** to `https://creme-supplier-bot.fly.dev/sms` (POST).
9. Watch it start. You should see "Scheduler started":
   ```
   fly logs
   ```
10. Prove a text gets in. **Not on a Wednesday:** after 9:00 on a Wednesday a freshly started bot asks for a count straight away, and the next count it gets sends an order (or an approval request). From your mobile, text a stock count to the Twilio number. A reply listing the counts proves that Twilio reached the bot, the auth token is right and the Anthropic key works. No reply, with "Invalid Twilio signature" in `fly logs`, means the request was refused: stop and fix that first.
11. Prove a text gets out. With all three numbers still set to your own mobile, run the manual trigger (see Testing, below). The approval request should arrive on your phone; reply YES and the order and the staff summary should follow. Only this proves the account SID, the Twilio number and outbound sending.
12. Clear the test and go live. Steps 10 and 11 leave a made-up count and a test order in the database, and the bot would treat them as real: it would skip an order for the next two days and work the next one out from the made-up count. Delete the database, then set the real numbers (in that order: setting a secret restarts the bot, and the restart recreates the database empty), and have staff text a true count before Wednesday:
    ```
    fly ssh console -C "rm /app/data/stock.db"
    fly secrets set 'SUPPLIER_PHONE_NUMBER=...' 'EMPLOYEE_PHONE_NUMBERS=...,...' 'APPROVER_PHONE_NUMBER=...'
    ```
    Only ever delete the database before go-live. After that it holds the café's order history and the record of the current week.

### After that

| To | Run |
|---|---|
| Deploy a change | `fly deploy --ha=false` |
| See what it is doing | `fly logs` |
| Change a secret (restarts the bot) | `fly secrets set 'NAME=value'` |
| End the approval trial | `fly secrets unset APPROVER_PHONE_NUMBER` |
| Switch the bot off | `fly scale count 0 --yes` (the volume and its data stay) |
| Switch it back on | `fly deploy --ha=false`, then `fly scale show` and `fly volumes list`: one machine, the same one volume, attached |
| Roll back | `fly releases --image`, then `fly deploy --ha=false --image <the previous image>` |

A deploy or a secret change restarts the bot. It picks up where it left off, but avoid doing it between 9:00 Wednesday and 9:00 Thursday, when an order may be waiting on a count or an approval.

Switching off and back on has not been rehearsed on Fly yet. Do it once before the café relies on the bot, not on a Wednesday, and note the volume's ID in `fly volumes list` before and after: the same ID, attached to the machine, is the proof the data is still there. Switching back on deploys the code in this folder, so be on `main` when you do it. If a second, empty volume appears, the bot has lost its memory of the week: switch it off again and sort that out first.

Fly snapshots the volume daily and keeps five days. `fly volumes snapshots list <volume id>` shows them.

**Cost:** not yet measured on Fly. Expect a few US dollars a month for the machine and volume; check Fly's pricing page.

---

## Testing

### Test stock parsing (no SMS sent)

```bash
python dry_run.py
```

### Manually trigger the weekly order job

```bash
python trigger_order.py
```

This fires the full order flow immediately — sends real SMS to supplier and employees.

On the deployed server the same job runs from `https://creme-supplier-bot.fly.dev/trigger`, with the key sent as a header so it is not written to the logs:

```
curl.exe -H 'X-Trigger-Key: <TRIGGER_KEY>' https://creme-supplier-bot.fly.dev/trigger
```

The key also works as `?key=` in the address, but then it is logged with the request; if that happens, change `TRIGGER_KEY`.
If this week's order (Wednesday to Tuesday) is already handled the job skips rather than ordering twice;
add `?force=1` to the address to send anyway. A forced send within 10 minutes of the last order is refused.

### Run unit tests

```bash
pytest
```

---

## Stock reporting format

Employees text the Twilio number in any natural format — Claude parses it:

```
Almond: 3 boxes, Oat: 1, Soy: 4, LF: 12 bottles, Coconut: 8 bottles
```

The system replies with a confirmation of what it recorded.

A message that doesn't contain a recognisable stock count is not recorded; the sender gets the format back.

If any item has no count from the last 3 days when Wednesday 9am comes, the system texts employees naming the items it needs and holds the order. The order goes out as soon as every item has a fresh count. If the count still hasn't come, it reminds employees 4 hours after asking (1pm Wednesday for the normal run). 24 hours after asking (9am Thursday) it closes the week: it says the order has not been placed and to order by hand. After that a count only records stock; it no longer places an order, so an order placed by hand is never doubled by a late text.

The system never orders twice for one shortfall. It assumes a delivery takes `DELIVERY_DAYS` (2, in `config.py` — **an assumption, confirm it with the supplier**) to arrive:

- If an order went out less than that long ago, a run places no order and texts employees that it was skipped.
- A count taken before that delivery was due doesn't count as fresh, because it can't include the delivery. The system asks employees to count again once the delivery has arrived.

In a normal week neither applies: the next count comes days after the last delivery.

If the job fails before the order is sent, employees get a text saying nothing went to the supplier, so the order can be placed by hand. If the send itself errors, the text says the order may not have reached the supplier and to check with them first — the system won't re-send it on its own.

---

## Approval during the trial

While `APPROVER_PHONE_NUMBER` is set, no order reaches the supplier until that person says yes.

1. When the order is worked out, it is texted to the approver with the stock counts it was worked out from. Staff get a text saying the order is waiting for approval.
2. The approver replies by text:
   - **Yes** (yes, y, yep, yeah, ok, sure, approve, confirm, "send it", "go ahead", 👍 👌 ✅, optionally with "thanks" or "please"): the order they were shown goes to the supplier and staff get the usual summary.
   - **No** (anything starting with no, nope, nah, don't, stop, cancel or reject, such as "no, too much oat", or 👎): nothing is sent. Staff are asked to count again, and the new order comes back to the approver. This repeats for every no. The approver's reply is kept, so the reason for a no is on record.
   - **Anything else**: nothing is sent and the system asks for YES or NO. A yes has to be the whole reply: "ok I'll check first", "yes make the oat 4", "ok?", "ok..." and "ok 🤔" do not send the order, and neither do "all good", "no worries" or "not sure". The approver cannot change quantities by text.
3. If the approver doesn't answer, they get one reminder after 4 hours. After 24 hours the order is not sent, and the approver and staff are told to order by hand. A yes after that sends nothing.
4. The approver also gets a text when a week ends with nothing for them to approve: no order was needed, or a count never came in.

The reply is matched against a fixed list of words. No AI reads it.

**The approver should not reply STOP or CANCEL.** The system reads those as a no, but Twilio may also treat them as an unsubscribe and block every later text to that number until they text START. (Twilio's behaviour here has not been tested on this account.)

To end the trial, remove `APPROVER_PHONE_NUMBER` from the environment (`fly secrets unset APPROVER_PHONE_NUMBER`, which restarts the bot): orders then go straight to the supplier as before. Do it when no order is waiting for approval. If one is waiting, it is cancelled at the restart, not sent, and staff are told to order by hand.

---

## Stock targets (configured in `config.py`)

| Item | Target | Unit |
|------|--------|------|
| Almond Milk | 12 | boxes |
| Oat Milk | 8 | boxes |
| Soy Milk | 7 | boxes |
| Lactose Free | 7 | bottles |
| Coconut | 5 | bottles |

Adjust these values in [config.py](config.py) to change what gets ordered.

---

## Seasonal adjustments

The order calculation adjusts quantities for the season. The uplift is applied to the shortfall (target minus stock), not to the target:

| Condition | Adjustment |
|-----------|-----------|
| Melbourne summer (Dec–Feb) | +20% |
| Victorian public holiday within 7 days | +20% |
| Both at once | +40% (capped) |

---

## Environment variables reference

See [.env.example](.env.example) for a full template.

---

## Cost estimate (fully operational)

| Service | Monthly |
|---------|---------|
| Anthropic Claude API | ~$0.18 |
| Twilio (number + SMS) | ~$2.75 |
| Fly.io hosting (Sydney) | not yet measured |
| **Total** | **not yet measured** |

The Anthropic and Twilio figures are estimates from April 2026 with a US number; an Australian number costs more.
