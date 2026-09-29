![Outis Python SDK](assets/header.png)

The Outis Python SDK asks real people to approve a risky operation before your code runs it.

## How Outis works

1. Your code asks Outis for approval and says what the approvers should see.
2. Outis shows that to your approvers on a physical device.
3. Enough of them turn their keys, or someone says no.
4. Your code runs the operation with its own credentials.

Outis never runs the operation, and it never sees your secrets, API keys or other credentials. It decides whether the right people approved, and keeps a record of who did.

## Install

```sh
pip install outis              # Python 3.10 or newer, no dependencies
pip install 'outis[durable]'   # adds sealed calls for workers (defer_to)
```

Set `OUTIS_API_KEY` in your environment, or pass `api_key=` to `Outis()`.

## Quickstart

```python
from outis import NotAuthorizedError, Outis, WaitTimeoutError

outis = Outis(requester="ops-cli")

approval = outis.guard(
    action="db.restore",
    show_approvers={"db": "payments", "snapshot": "2026-09-26"},
    wait="10m",
)

try:
    approval.result()                   # blocks until someone decides
    restore("payments", "2026-09-26")
except (NotAuthorizedError, WaitTimeoutError) as exc:
    print(f"not running the restore: {exc}")
```

`guard` returns right away. The SDK polls Outis on a background thread, and
`approval` is a standard `concurrent.futures.Future`. To keep going without
waiting, add a callback. It runs on the background thread.

```python
def on_done(f):
    if not f.cancelled() and f.exception() is None:
        restore("payments", "2026-09-26")

approval.add_done_callback(on_done)
```

Approvers see `action`, `summary` if you give one, and `show_approvers`,
exactly as written. The values must be strings. In asyncio code, use
`await outis.guard_async(...)` instead. It waits on the running event loop.

## Pick a path

| Your situation | Use |
|---|---|
| Approval usually comes in minutes | `guard(..., wait=...)` |
| Approval could take hours or days | `guard(..., defer_to=...)` plus a worker |
| You want to guard a method you already call | `guard_method` |
| You're moving money with Stripe | `outis.recipes.stripe` |

`wait` is capped at 30 minutes. The wait lives in your process, so if the
process exits first, nothing runs after approval. Anything that has to
survive a restart should use `defer_to`.

`approval.cancel()` stops the local polling. It doesn't withdraw the request,
which stays open in Outis until someone decides or it expires.

## Hand the call to a worker: `defer_to`

With `defer_to`, `guard` records the exact call to make, encrypts it with your
intent key, and returns once the request exists. Nothing runs yet.

```python
deferred = outis.guard(
    action="payouts.send",
    show_approvers={"to": "acct_9f2", "amount": "2500.00 USD"},
    defer_to={"worker": "payouts", "call": "send", "args": ["acct_9f2", 250000]},
)
print(deferred.id)
```

`worker` is the name a worker registers a client under, and `call` is the
method to run on it. `args` and `kwargs` must be plain JSON. Generate an
intent key with `python -m outis keygen` and set it as `OUTIS_INTENT_KEY`
wherever you call `guard` and wherever the worker runs.

A worker picks up each approved call, checks it against what the approvers
saw, and runs it:

```python
from outis import Outis

worker = Outis().worker(clients={"payouts": payouts_client})
worker.start()   # polls every 15 seconds and stops cleanly on SIGTERM or Ctrl-C
```

On a stop signal, `start()` finishes the calls already running and then
returns. `python -m outis init worker` writes a starter for a plain process,
FastAPI, Celery or Temporal.

When no client covers the call, register a handler. It gets an
`ExecutionContext` first, then the call's own arguments, positional and
keyword alike:

```python
def restore(ctx, db, *, snapshot):
    return backups.restore(db, snapshot, idempotency_key=ctx.idempotency_key)

worker = Outis().worker(handlers={"db.restore": restore})
```

Running on cron or a serverless function? Nothing stays up to loop, so call
`worker.poll()` on each invocation. It runs whatever's approved right now,
waits for it, and returns the results.

## Guard a method: `guard_method`

`guard_method` wraps one method of an object you already use. Each option can
be a function of the call's arguments.

```python
send = outis.guard_method(
    payouts_client,
    "send",
    action="payouts.send",
    show_approvers=lambda to, cents: {"to": to, "amount": str(cents)},
    wait="5m",
)
receipt = send("acct_9f2", 250000)
```

With `wait`, each call waits for approval, then runs the real method and
returns its result. If the approvers say no, the method never runs. With
`defer_to={"worker": ..., "call": ...}`, each call returns a `Deferred` and
the worker runs it later. `guard_method_async` does the same for asyncio
code and awaits async methods.

## Stripe recipes

Recipes pick the fields that are safe to show on a device and worth
approving: ids (the connected account too), amounts in minor units,
currency, settings like the refund reason, and a description cut to 64
characters. They never include metadata, emails or card data.

```python
from stripe import StripeClient
from outis.recipes import stripe as recipes

client = StripeClient(stripe_key)
create_transfer = outis.guard_method(
    client.v1.transfers, "create", **recipes.transfers.create(), wait="5m"
)
create_transfer({"amount": 2500000, "currency": "usd", "destination": "acct_9f2"})
```

The recipes are `transfers.create()`, `payouts.create()`,
`refunds.create()` and `customers.delete()`. You still choose `wait` or
`defer_to`, plus `requester` if the client has no default. Each recipe also
names the method a worker calls, so `defer_to={"worker": "stripe"}` is enough
when the worker registers `clients={"stripe": client.v1}`.

## Errors

- `NotAuthorizedError`: the request ended without approval. `exc.outcome` is
  `denied`, `expired` or `aborted`.
- `WaitTimeoutError`: `wait` ran out first. The request is still open, and
  `exc.request_id` lets you check on it later.
- `ValueError` or `TypeError`: a bad option, raised before anything is sent.
  That includes passing both `wait` and `defer_to`, or neither.
- `APIError` and its subclasses: Outis refused the call or couldn't be reached.

## Learn more

The full guides live at [developers.outis.tech](https://developers.outis.tech):

- [The SDKs](https://developers.outis.tech/guides/sdks/)
- [The full API](https://developers.outis.tech/api/)
- [Webhooks](https://developers.outis.tech/guides/webhooks/)
- [Durable execution](https://developers.outis.tech/guides/durable-execution/)
- [Workflow engines](https://developers.outis.tech/guides/engines/)
- [The low-level request API](https://developers.outis.tech/guides/create/)

For full control, use the low-level layer:
`outis.requests` (`create`, `retrieve`, `wait_for`, `assert_authorized`),
`outis.webhooks.verify`, `callback_secret`, and the worker's `execute`,
`run` and webhook handlers.
