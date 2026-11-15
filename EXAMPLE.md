# Example: one task, all five message types

The project is `shop-api`. The owner wants to stop password guessing on the login endpoint.
The owner speaks English in this example. In another language only the labels and the owner's sentences change.

## 1. DECISION (lead to owner)

```
DECISION   Do we set the login limit in the app and not in the proxy?
RECOMMEND  The app. One change covers every environment.
COST       One new module. One revert undoes it.
OTHERS     A proxy rule: no code change, but staging has no proxy.
IF GO      I write the contract and start the worker.
```

The owner answers: "GO, 5 tries".

```
DEC-20261115-01 | shop-api | Rate limit in the app | "GO, 5 tries" | 2026-11-15 | standing | -
```

## 2. CONTRACT (lead to worker)

```
CON-20261115-01  Limit failed logins on POST /login.
ACC-1  When a client fails 5 logins in 1 minute, the API shall answer 429 for the next minute.  Check: npm test -- rate-limit
ACC-2  While a client is limited, the API shall not query the user table.                       Check: npm test -- rate-limit
ACC-3  If the limiter store is down, then the API shall allow the login and log a warning.      Check: npm test -- limiter-down
Out of scope: the password reset endpoint, the proxy config
```

The lead stores the contract as `records/CON-20261115-01.md`.

## 3. HANDOFF (worker to lead)

```
id        HND-20261115-01
from      worker
date      2026-11-15
state     PARTIAL
readback  task: limit failed logins on POST /login | limits: no change to reset or proxy | done when: ACC-1, ACC-2, ACC-3
summary   The limiter works for ACC-1 and ACC-2. ACC-3 has no test.
changes   src/auth/login.ts: calls the limiter before the user query
          src/auth/limiter.ts: new, sliding window for each client IP
commit    shop-api@4f2a91c
checks    npm test -- rate-limit | exit 0 | 6 tests passed
contract  ACC-1 VERIFIED | ACC-2 VERIFIED | ACC-3 UNVERIFIED (no test for a store that is down)
open      Write the limiter-down test.
issues    high: the limiter trusts the X-Forwarded-For header. A client can change it.
next      Decide if the proxy sets the client IP.
```

The readback arrived in the `readback` key. This is the recorded form.
The lead stores the handoff as `records/HND-20261115-01.md`.

## 4. Judge (a different agent)

```
PASS  The diff and the test output support ACC-1 and ACC-2. ACC-3 is correctly UNVERIFIED.
```

PASS is about the handoff. The state is PARTIAL, so the lead tells the owner what remains.
The lead adds two lines below the stored handoff.

```
JUDGE     PASS | judge | shop-api@4f2a91c | —
ACCEPTED  no | 2026-11-15 | The state is PARTIAL. ACC-3 is UNVERIFIED.
```

## 5. LOG (lead to record)

```
LOG-20261115-01 | shop-api | code:shop-api@4f2a91c | Login answers 429 after 5 failed tries in 1 minute. | Stop password guessing. | DEC-20261115-01 "GO, 5 tries" | HND-20261115-01
```

## 6. BRIEF (lead to owner)

```
DID        Login now answers 429 after 5 failed tries in 1 minute. You said "GO, 5 tries".
NEEDS YOU  Should the proxy set the client IP? Today a client can change one header and pass the limit.
KNOW       No test covers a limiter store that is down (UNVERIFIED).
OPEN       The test that allows the login when the store is down.
Next:      Decide the client IP question. Then I start the missing test.
```

## 7. CURRENT (lead to record)

The lead writes `CURRENT.md` after the brief.

```
CURRENT   shop-api
updated   2026-11-15 | after LOG-20261115-01
GOAL      Stop password guessing on the login endpoint.
IN FORCE  DEC-20261115-01 rate limit in the app, 5 tries
OPEN      The owner decides if the proxy sets the client IP. (owner)
          The limiter-down test, ACC-3 of CON-20261115-01. See HND-20261115-01.
NEXT      Ask the owner the client IP question.
```

## 8. The next session

The owner starts a new session and writes: "Continue where we stopped."
The lead reads `CURRENT.md`, the newer LOG lines, `HND-20261115-01` and the state of the repository. Then it answers.

```
Goal: stop password guessing on login. The limit is in the app, 5 tries (DEC-20261115-01).
Open: you decide if the proxy sets the client IP. No test covers a limiter store that is down.
Next: decide the client IP question. Then I start the missing test.
```

The owner answers: "The proxy sets it."

```
DEC-20261116-01 | shop-api | The proxy sets the client IP. | "The proxy sets it." | 2026-11-16 | standing | -
```
