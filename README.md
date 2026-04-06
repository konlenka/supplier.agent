# Creme Cafe — Automated Milk Ordering System

Automated inventory and ordering system for Creme Cafe (70-72 Bay Street, Melbourne). Employees text in stock levels via SMS. Every Wednesday at 9am, the system automatically calculates and sends a milk order to the supplier.

---

## How it works

```
Employee texts stock levels (SMS)
        ↓
Claude AI parses the message → saves to local database
        ↓
Wednesday 9:00 AM AEST (automatic)
        ↓
Claude AI checks stock, targets, season & order history
        ↓
Texts the order to supplier
        ↓
Texts a confirmation summary to all employees
```

No human input required on ordering day — it runs fully automatically.

---

## Stack

| Component | Technology |
|-----------|-----------|
| Web server / SMS webhook | Flask |
| SMS (inbound + outbound) | Twilio |
| AI parsing + ordering logic | Anthropic Claude (Haiku + Sonnet) |
| Database | SQLite |
| Scheduling | APScheduler |

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/creme.git
cd creme
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
```

| Variable | Where to get it |
|----------|----------------|
| `TWILIO_ACCOUNT_SID` | [Twilio Console](https://console.twilio.com) → Account Info |
| `TWILIO_AUTH_TOKEN` | Twilio Console → Account Info |
| `TWILIO_PHONE_NUMBER` | The Australian number you buy in Twilio |
| `SUPPLIER_PHONE_NUMBER` | Your milk supplier's mobile number |
| `EMPLOYEE_PHONE_NUMBERS` | Comma-separated staff numbers who report stock |
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

## Deployment (Railway)

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and create a new project from your GitHub repo
3. Add all your `.env` variables in Railway's **Variables** tab
4. Railway auto-detects Flask and deploys — you'll get a permanent public URL
5. Update your Twilio webhook URL to the Railway URL: `https://your-app.railway.app/sms`

**Cost:** ~$0.50–$2/month (well within Railway's $5/month Hobby credit).

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

If no stock report is received within 3 days of Wednesday, the system automatically texts employees asking for an update before placing the order.

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

The ordering agent automatically adjusts quantities based on demand:

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
| Railway hosting | ~$0.50–2.00 |
| **Total** | **~$3.50–5/month** |
