# Example: one task, all five message types

The project is `shop-api`. The owner wants to stop password guessing on the login endpoint.
The owner speaks Turkish. Records are English.

## 1. DECISION (lead to owner, in the owner's language)

```
Karar          Giriş limitini proxy'de değil uygulamada mı kuralım?
Öneri          Uygulama. Tek değişiklik bütün ortamları kapsar.
Bedel          Yeni bir modül. Tek revert ile geri alınır.
Diğer seçenek  Proxy kuralı: kod değişmez, ama staging'de proxy yok.
GO olursa      Sözleşmeyi yazar, işçiyi başlatırım.
```

The owner answers: "GO, 5 deneme olsun".

```
DEC-20261115-01 | shop-api | Rate limit in the app | "GO, 5 deneme olsun" | 2026-11-15
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

## 5. LOG (lead to record)

```
LOG-20261115-01 | shop-api | code:shop-api@4f2a91c | Login answers 429 after 5 failed tries in 1 minute. | Stop password guessing. | DEC-20261115-01 "GO, 5 deneme olsun" | HND-20261115-01
```

## 6. BRIEF (lead to owner, in the owner's language)

```
Yapılan          Login artık 1 dakikada 5 hatalı denemeden sonra 429 dönüyor. Sen "GO, 5 deneme olsun" dedin.
Senden beklenen  İstemci IP'sini proxy mi belirlesin? Şu an istemci bir başlığı değiştirip limiti aşabilir.
Bilmen gereken   Limit deposu çökerse ne olacağı test edilmedi (UNVERIFIED).
Yapılmayan       Depo çöktüğünde girişe izin veren testin yazılması.
Sonraki adım     IP kararını ver; ardından eksik testi yazdırırım.
```

The same brief for an owner who speaks English. The quote stays as the owner typed it.

```
DID        Login now answers 429 after 5 failed tries in 1 minute. You said "GO, 5 deneme olsun".
NEEDS YOU  Should the proxy set the client IP? Today a client can change one header and pass the limit.
KNOW       No test covers a limiter store that is down (UNVERIFIED).
OPEN       The test that allows the login when the store is down.
Next:      Decide the client IP question. Then I start the missing test.
```
