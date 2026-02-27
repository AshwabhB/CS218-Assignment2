# Serverless Order API with Idempotency
## CS-218 Cloud Computing — Assignment 2 - Serverless Order API: Idempotency, Retries, and Eventual Consistency

 ### Deployed on AWS EC2.
 ### Objective

In cloud systems, requests may be delivered more than once due to timeouts, network failures, or automatic retries. Infrastructure typically guarantees at-least-once delivery, not exactly-once execution.

The goal of this assignment is to design and deploy a cloud API that remains correct under retries and partial failures, and to reason about eventual consistency in distributed environments.

I have implemented application-level mechanisms to ensure exactly-once effects even when the same request is delivered multiple times.

---

## Project Structure

```
Ash-orders/
├── app.py              # FastAPI routes, middleware, idempotency logic, logging
├── database.py         # SQLite schema, atomic transactions, queries
├── requirements.txt    # Python dependencies
└── README.md           # This file
```

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | /orders | Create an order (idempotent) |
| GET | /orders/{order_id} | Retrieve a single order |
| GET | /orders | List all orders |
| GET | /ledger | List all ledger entries |
| GET | /health | Health check |
| DELETE | /reset | Clear database |

## Database Schema

**`orders`**
| Column | Type | Purpose |
|--------|------|---------|
| order_id | TEXT (UUID) | Primary key |
| customer_id | TEXT | Who placed the order |
| item_id | TEXT | What was ordered |
| quantity | INTEGER | How many |
| status | TEXT | Always "created" |
| created_at | TEXT (ISO timestamp) | When the order was created |

**`ledger`**
| Column | Type | Purpose |
|--------|------|---------|
| ledger_id | TEXT (UUID) | Primary key |
| order_id | TEXT | Links to the order |
| customer_id | TEXT | Who was charged |
| amount | REAL | Dollar amount |
| type | TEXT | Always "charge" |
| created_at | TEXT | When the charge was created |

**`idempotency_records`**
| Column | Type | Purpose |
|--------|------|---------|
| idempotency_key | TEXT | Primary key |
| request_fingerprint | TEXT | SHA-256 hash of request body |
| response_body | TEXT | Stored JSON response |
| response_status_code | INTEGER | Stored HTTP status code |
| created_at | TEXT | When the record was created |

---

## Deployment

### EC2 Instance Configuration

| Setting | Value |
|---------|-------|
| AMI | Ubuntu Server 24.04 LTS |
| Instance type | t3.micro (Free tier) |
| Storage | 8 GiB gp3 |

### Security Group Rules

| Type | Port | Source |
|------|------|--------|
| SSH | 22 | Your IP or 0.0.0.0/0 (I chose this, either is fine)|
| Custom TCP | 8000 | 0.0.0.0/0 |
| HTTP | 80 | 0.0.0.0/0 |

### 1. Connect via SSH

```bash
chmod 400 ~/Downloads/Ash-orders-key.pem
ssh -i ~/Downloads/Ash-orders-key.pem ubuntu@EC2_PUBLIC_IP
```

### 2. Install Dependencies

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

### 3. Clone the Repository

```bash
git clone https://github.com/AshwabhB/CS218-Assignment2.git ~/Ash-orders
cd ~/Ash-orders
```

### 4. Set Up Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 5. Start the Server

```bash
nohup uvicorn app:app --host 0.0.0.0 --port 8000 > server.log 2>&1 &
```

Verify it's running:

```bash
curl http://localhost:8000/health
```

---

## Verification Tests

Replace `EC2_PUBLIC_IP` with your EC2 instance's public IP:

```bash
export BASE_URL="http://EC2_PUBLIC_IP:8000"
```

**Reset database (clean slate):**

```bash
curl -s -X DELETE $BASE_URL/reset
```

**Step 1 — Create an order:**

```bash
curl -s -w "\nHTTP Status: %{http_code}\n" -X POST $BASE_URL/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```
```bash
Expected output:
{"order_id":"<some-uuid>","status":"created"}
HTTP Status: 201
```


**Step 2 — Retry with same key (idempotent replay):**

```bash
curl -s -w "\nHTTP Status: %{http_code}\n" -X POST $BASE_URL/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":1}'
```
```bash
Expected output:
Exact same order_id and 201 status. No duplicate was created.
```

**Step 3 — Same key, different payload (409 Conflict):**

```bash
curl -s -w "\nHTTP Status: %{http_code}\n" -X POST $BASE_URL/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-123" \
  -d '{"customer_id":"cust1","item_id":"item1","quantity":5}'
```
```bash
Expected output:
'{"detail":{"error":"Idempotency key conflict",...}}'
HTTP Status: 409
```

**Step 4 — Simulated failure after commit:**

```bash
curl -s -w "\nHTTP Status: %{http_code}\n" -X POST $BASE_URL/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fail-1" \
  -H "X-Debug-Fail-After-Commit: true" \
  -d '{"customer_id":"cust2","item_id":"item2","quantity":1}'
```
```bash
Expected output:
'{"detail":{"error":"Simulated failure",...}}'
HTTP Status: 500
```

**Step 5 — Verify Order was still created. :**

```bash
curl -s $BASE_URL/orders | python3 -m json.tool
```
```bash
Expected output:
We see two orders. 
```

**Step 6 — Retry after failure:**

```bash
curl -s -w "\nHTTP Status: %{http_code}\n" -X POST $BASE_URL/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: test-fail-1" \
  -d '{"customer_id":"cust2","item_id":"item2","quantity":1}'
```
```bash
Expected output:
Same as before, when we tried recreating our first order, this time too, it fetched the same order ID and status that was committed in the database from the last step (simulated failure).
```

**Step 7 — Verify no duplicates:**

```bash
curl -s $BASE_URL/orders | python3 -m json.tool
```

```bash
curl -s $BASE_URL/ledger | python3 -m json.tool
```
```bash
Expected: exactly 2 orders and 2 ledger entries.
```