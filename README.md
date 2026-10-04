# SportsClan API

Django REST API for sports, local venues, tournaments, and player spots. Tournaments store an entry price and a fixed number of numbered spots. JWT access tokens protect account and tournament actions.

## Run locally

Use Python 3.11 or newer. In PowerShell, from this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py seed_catalog
python manage.py seed_countries
python manage.py createsuperuser
python manage.py runserver
```

The API runs at `http://127.0.0.1:8000/api/v1/`. `seed_countries` loads ISO countries and their current tender currencies. Add local stadiums, courts, and fields in Django Admin at `http://127.0.0.1:8000/admin/`, assign each venue its country, then connect it to its supported sports. SQLite is used by default. Set `DATABASE_URL` to use PostgreSQL.

For each venue, Admin supports `free`, `fixed`, `hourly`, and `quote` pricing. Free venues use zero; fixed and hourly rates need an amount and currency; quote venues leave the rate blank. Tournaments store their duration and a snapshot of the selected venue rate. The API returns `venue_fee_estimate`; hourly estimates use `rate × duration_minutes / 60`. Hosts can PATCH `venue_fee` and `venue_fee_status: "agreed"` after the venue confirms the booking. That records an agreement only; it does not verify or process payment.

## Deploying on Render

The web service must apply Django migrations before serving API requests. Set its Render start command to:

```sh
python manage.py migrate --noinput && python manage.py seed_catalog && python manage.py seed_countries && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT
```

Errors such as `no such table: tournaments_country` mean migrations were not applied to the database used by the running service. For an immediate repair, run `python manage.py migrate --noinput`, `python manage.py seed_catalog`, and `python manage.py seed_countries` in that service's Render Shell, then restart it.

The settings fall back to SQLite when `DATABASE_URL` is absent. Render's web-service filesystem is ephemeral unless a persistent disk is mounted, so configure a managed PostgreSQL database and set `DATABASE_URL` to keep accounts and game data across deploys/restarts. Configure the start command above after connecting the database.

## API endpoints

| Method | Endpoint | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/auth/register/` | No | Create an account and receive access/refresh tokens |
| `POST` | `/api/v1/waitlist/` | No | Join the player, organizer, or app-tester waitlist |
| `POST` | `/api/v1/auth/token/` | No | Log in with username and password |
| `POST` | `/api/v1/auth/token/refresh/` | No | Get a new access token |
| `GET` | `/api/v1/auth/me/` | Yes | Get the signed-in user |
| `GET, PATCH` | `/api/v1/profiles/me/` | Yes | Read or update player details and preferred sports |
| `GET` | `/api/v1/players/{username}/` | No | View a player card and completed-game history |
| `GET, POST` | `/api/v1/notifications/` | Yes | Read notifications or mark all as read |
| `POST` | `/api/v1/reports/` | Yes | Report one game or player for admin review |
| `GET` | `/api/v1/sports/` | No | List active sports |
| `GET` | `/api/v1/countries/` | No | List countries, default currencies, and supported currencies |
| `GET` | `/api/v1/venues/?city=Springfield&sports__slug=basketball` | No | Find local venues |
| `GET, POST` | `/api/v1/tournaments/` | POST requires auth | Browse or create tournaments |
| `GET, PATCH, DELETE` | `/api/v1/tournaments/{id}/` | Changes require host | View or manage a tournament |
| `POST` | `/api/v1/tournaments/{id}/join/` | Yes | Claim an open numbered spot |
| `GET` | `/api/v1/tournaments/{id}/participants/` | No | View joined players and spot/payment states |
| `GET, POST, DELETE` | `/api/v1/tournaments/{id}/waitlist/` | Yes | Join, inspect, or leave the queue for a full game |
| `GET, POST` | `/api/v1/tournaments/{id}/messages/` | Yes | Read game updates; hosts can send announcements |
| `POST` | `/api/v1/tournaments/{id}/cancel/` | Host | Cancel a game and notify players |
| `POST` | `/api/v1/tournaments/{id}/complete/` | Host | Mark an ended game complete for player history |
| `DELETE` | `/api/v1/tournaments/{id}/leave/` | Yes | Leave before the entry is paid |
| `GET` | `/api/v1/tournaments/{id}/payment-status/` | Yes | Read the player's reservation and payment status |
| `POST` | `/api/v1/tournaments/{id}/payment-initialize/` | Yes | Create or reuse a Paystack checkout |
| `POST` | `/api/v1/tournaments/{id}/payment-verify/` | Yes | Verify a Paystack reference and secure a paid spot |
| `POST` | `/api/v1/payments/paystack/webhook/` | Paystack signature | Process signed `charge.success` events |
| `GET` | `/api/v1/tournaments/mine/` | Yes | List tournaments you host or joined |
| `GET` | `/api/schema/` | No | OpenAPI schema |

Lists are paginated with a `results` array. Tournament lists can be filtered by `status`, `sport__slug`, or `venue__city`, searched with `search`, and ordered by `starts_at`, `entry_fee`, or `created_at`. The `starts_after` and `starts_before` parameters filter start times.

Waitlist emails are normalized and deduplicated. The public endpoint is rate-limited to 10 submissions per hour per IP. The production website origin is `https://sportsclanui.vercel.app` (without a trailing slash); include it in the deployed `CORS_ALLOWED_ORIGINS` setting because an environment value overrides the code default. Local defaults include ports 3000 and 3102.

Tournament responses include `taken_slots` (paid entries and active reservations), `slots_open`, `my_slot` (the signed-in user's spot, or `null`), `my_payment_status`, and `projected_prize_pool` (`entry_fee × max_players`). A pending paid entry holds its numbered spot for 15 minutes. Expired unpaid reservations are released when another player claims a spot.

Venue-aware tournament responses also include `duration_minutes`, `venue_pricing_type_snapshot`, `venue_rate_snapshot`, `venue_rate_currency_snapshot`, `venue_fee_estimate`, `venue_fee`, and `venue_fee_status`. Venue estimates are displayed separately from player entry fees and projected prize pools. Hosts pay venue costs separately in this MVP.

## Mobile examples

Register:

```http
POST /api/v1/auth/register/
Content-Type: application/json

{
  "username": "sam",
  "email": "sam@example.com",
  "password": "a-long-password"
}
```

Create a tournament with the returned access token:

```http
POST /api/v1/tournaments/
Authorization: Bearer <access-token>
Content-Type: application/json

{
  "title": "Sunday Hoops",
  "description": "Bring a light and dark shirt.",
  "country_id": "US",
  "sport_id": 1,
  "venue_id": 1,
  "starts_at": "2026-11-01T16:00:00Z",
  "entry_fee": "5.00",
  "currency": "USD",
  "max_players": 8
}
```

Join a numbered spot:

```http
POST /api/v1/tournaments/1/join/
Authorization: Bearer <access-token>
Content-Type: application/json

{
  "slot_number": 3
}
```

Successful paid joins save the fee at the time of joining and return a 15-minute reservation with `payment_status: "pending"`. Free joins use `payment_status: "not_required"`.

### Paystack checkout flow

1. The mobile app reserves a numbered spot with `POST /join/`.
2. It calls `POST /payment-initialize/`. The server derives the amount, email, and currency from the signed-in user and tournament, creates a unique reference, and initializes Paystack using `PAYSTACK_SECRET_KEY`. The secret key must never be sent to the app.
3. The app opens the returned `authorization_url` and, after checkout, calls `POST /payment-verify/` with the returned reference. A browser redirect alone is not proof of payment.
4. The server calls Paystack's verify endpoint and checks success, reference, amount, and currency before marking the participant paid. The signed webhook is an additional confirmation path.

Set `PAYSTACK_SECRET_KEY` on the backend to a Paystack test secret while developing. Configure Paystack to send `charge.success` events to `https://<api-host>/api/v1/payments/paystack/webhook/`. Use HTTPS in deployed environments. Checkout currently accepts NGN, GHS, ZAR, KES, and USD. If a payment succeeds after its 15-minute hold has expired, the API records `success_unallocated` rather than taking another player's spot; that payment requires support/refund handling.

Paid entries cannot leave until refunded. Hosts manage venues and sports through Admin in this first version.

## Tests

```powershell
python manage.py check
python manage.py test
```