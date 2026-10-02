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

## API endpoints

| Method | Endpoint | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/v1/auth/register/` | No | Create an account and receive access/refresh tokens |
| `POST` | `/api/v1/auth/token/` | No | Log in with username and password |
| `POST` | `/api/v1/auth/token/refresh/` | No | Get a new access token |
| `GET` | `/api/v1/auth/me/` | Yes | Get the signed-in user |
| `GET` | `/api/v1/sports/` | No | List active sports |
| `GET` | `/api/v1/countries/` | No | List countries, default currencies, and supported currencies |
| `GET` | `/api/v1/venues/?city=Springfield&sports__slug=basketball` | No | Find local venues |
| `GET, POST` | `/api/v1/tournaments/` | POST requires auth | Browse or create tournaments |
| `GET, PATCH, DELETE` | `/api/v1/tournaments/{id}/` | Changes require host | View or manage a tournament |
| `POST` | `/api/v1/tournaments/{id}/join/` | Yes | Claim an open numbered spot |
| `DELETE` | `/api/v1/tournaments/{id}/leave/` | Yes | Leave before the entry is paid |
| `GET` | `/api/v1/tournaments/mine/` | Yes | List tournaments you host or joined |
| `GET` | `/api/schema/` | No | OpenAPI schema |

Lists are paginated with a `results` array. Tournament lists can be filtered by `status`, `sport__slug`, or `venue__city`, searched with `search`, and ordered by `starts_at`, `entry_fee`, or `created_at`. The `starts_after` and `starts_before` parameters filter start times.

Tournament responses include `taken_slots` (the occupied numbered spots), `slots_open`, `my_slot` (the signed-in user's spot, or `null`), and `projected_prize_pool` (`entry_fee × max_players`). The projected pool assumes every spot is paid; actual money is not collected or distributed by this API yet.

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

Successful joins save the fee at the time of joining and begin with `payment_status: "pending"`. The API reserves spots but does not charge money; connect a payment provider before treating a fee as collected. Paid entries cannot leave until refunded. Hosts manage venues and sports through Admin in this first version.

## Tests

```powershell
python manage.py check
python manage.py test
```