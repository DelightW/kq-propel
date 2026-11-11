# KQ-Propel: integration request to Kenya Airways IT

**Status:** proposal. KQ-Propel already contains adapters for the two services below, tested against stubbed responses in exactly these shapes (`backend/tools/test_kq_integration.py`). If Kenya Airways' services differ, only the mapping function in each adapter changes; the assistant itself does not.

Until the services are provided, the assistant uses clearly labelled demonstration data: three sample bookings (`KQDEMO` / Wanjiru, `KQBIZ1` / Wambui and `KQDLW1` / Wangui) and three sample flights.

## 1. Booking lookup (`backend/app/booking.py`)

**Purpose.** With the passenger's booking reference and surname, the assistant answers for their actual trip: the baggage allowance on their fare, the check-in rules for their route (domestic or international), and the status of their flight. This is also where IATA NDC order data enters the system.

**Request.** `POST {KQ_BOOKING_API_URL}/orders/retrieve`, header `Authorization: Bearer {KQ_BOOKING_API_TOKEN}`

```json
{ "pnr": "AB12CD", "surname": "Kanja" }
```

POST is used so that the surname never appears in a URL or access log.

**Response 200.** A simplified NDC order view. Only the fields shown are used.

```json
{
  "pnr": "AB12CD",
  "passengers": [{ "givenName": "Delight", "surname": "Kanja" }],
  "segments": [{
    "flightNumber": "KQ412",
    "origin":      { "iata": "NBO", "city": "Nairobi" },
    "destination": { "iata": "MBA", "city": "Mombasa" },
    "departureUtc": "2026-12-01T06:00:00Z",
    "cabin": "Economy",
    "fareBrand": "Economy Saver",
    "checkedBaggage": { "pieces": 1, "weightKg": 23 }
  }]
}
```

**Response 404.** No booking matches that reference **and** surname. Please do not distinguish "reference not found" from "surname does not match"; the assistant gives the passenger one answer for both.

**What the assistant does with it.**

- Passenger names (`passengers[].givenName`, `passengers[].surname`) are needed for name corrections. They are kept with the segments for 30 minutes of conversation memory, never written to the staff-visible trace, and deleted with the conversation.
- A paid name correction becomes a request on the staff dashboard; staff update the booking in the reservation system and mark it done. A future `POST /orders/{pnr}/name-correction` endpoint could replace that manual step.
- After five failed lookups within an hour, a session is refused further lookups.
- Errors and timeouts (8 seconds) are reported to the passenger as "booking system unavailable". The assistant never guesses a booking.

**Questions for Kenya Airways.**

1. Which system serves this (the reservation system's NDC OrderRetrieve, or an internal API in front of it)?
2. Network access: an allow-listed IP address, mutual TLS or an API gateway?
3. Should the lookup itself be logged on Kenya Airways' side, for example for fraud monitoring?

## 2. Flight status (`backend/app/kq_flights.py`)

**Purpose.** To show the same status, gate and delay as Kenya Airways' own systems and the airport screens. AviationStack, the current source, is a third-party aggregator and can lag.

**Request.** `GET {KQ_FLIGHT_API_URL}/flights/{flightNumber}?date=YYYY-MM-DD`, header `Authorization: Bearer {KQ_FLIGHT_API_TOKEN}`

**Response 200.**

```json
{
  "flightNumber": "KQ100",
  "status": "DELAYED",
  "delayMinutes": 40,
  "scheduledDepartureUtc": "2026-11-10T22:40Z",
  "estimatedDepartureUtc": "2026-11-10T23:20Z",
  "departure": { "iata": "NBO", "name": "Jomo Kenyatta International", "gate": "C3" },
  "arrival":   { "iata": "LHR", "name": "London Heathrow" }
}
```

Fields Kenya Airways does not have may be omitted. The assistant then says nothing about them; it never reports a missing delay as zero.

**Response 404.** The flight does not operate on that date. The assistant tells the passenger so and does not consult AviationStack, because the airline is the authority on its own schedule.

**Failure.** If the feed itself is down, the assistant uses AviationStack when a key is configured, naming it as the source; otherwise it says the status is unavailable.

## 3. Settings

```
KQ_BOOKING_API_URL=        KQ_BOOKING_API_TOKEN=
KQ_FLIGHT_API_URL=         KQ_FLIGHT_API_TOKEN=
KQ_API_TIMEOUT_SECONDS=8   BOOKING_LOOKUP_MAX_FAILURES=5
```

Tokens are read from the server's environment and are never sent to the browser.
