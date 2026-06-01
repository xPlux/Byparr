# Plan: pula przeglądarek + warm pool + lock-then-respond

Plan implementacji uzgodniony po review. Zastępuje wcześniejszy dokument
analityczny. Odnośniki do kodu: [src/utils.py](../src/utils.py),
[src/endpoints.py](../src/endpoints.py), [src/consts.py](../src/consts.py),
[main.py](../main.py).

Cel: zamienić obecny model „jedna współdzielona instancja + flaga `busy`" na
**pulę slotów** z opcjonalnym **warm poolem** i oddawaniem odpowiedzi przed
recyklingiem przeglądarki (lock-then-respond).

---

## Ustalenia (decyzje projektowe)

1. **Lock-then-respond.** Slot jest zwalniany (lock zdjęty) i odpowiedź jest
   wysyłana **przed** teardownem/respawnem przeglądarki. Recykling leci w tle.
2. **Cookies przy `ROTATE_EVERY > 1`.** Zachowanie obecne jest poprawne i
   zamierzone: przekazane cookie nadpisują (upsert po `name+domain+path`),
   reszta stanu kumuluje się celowo. Bez zmian.
3. **`FINGERPRINT_CLEAR_BETWEEN` default → `false`** (zmiana z `true`). Reużycie
   ma sens po to, by trzymać `cf_clearance`; czyszczenie zabijało ten zysk.
4. **Błąd → natychmiastowy teardown + rotacja**, ignorując licznik. Już działa,
   przenieść 1:1 do modelu slotów: zepsuty slot nie wraca do puli.
5. **Pula slotów (`MAX_BROWSERS`).** `1` = dziś; `N>1` = do N równoległych
   browserów; `-1` = bez limitu (= zachowanie upstreamu sprzed forka). Każdy
   slot ma własny licznik rotacji; wejściowe `FINGERPRINT_ROTATE_EVERY` wspólne.
6. **Warm pool (`WARM_BROWSERS`, int).** Liczba bezczynnych, gotowych browserów
   utrzymywanych na zapas. `0` = w pełni leniwe (zero idle-RAM). Niezmiennik:
   `idle ≈ WARM_BROWSERS`, przy `total (idle+busy+spawning) ≤ MAX_BROWSERS`.
7. **Teardown timeout z budżetu requestu, nie stała.** Zamiast sztywnego
   `BROWSER_SHUTDOWN_TIMEOUT`: clamp `max_timeout` do `MAX_TIMEOUT_CAP` (60 s) i
   liczyć `teardown_timeout = effective_max_timeout + BROWSER_SHUTDOWN_MARGIN`.
   Slot zapamiętuje `max_timeout` ostatniego żądania (potrzebne do recyklingu w
   tle). Brak osobnego górnego sufitu — clamp maxTimeout go zastępuje.

---

## Model slotu i niezmienniki

Slot = niezależny stan przeglądarki:

```python
class BrowserSlot:
    camoufox: AsyncCamoufox | None
    context: BrowserContext | None
    page: Page | None
    profile_dir: str | None
    uses_left: int            # własny licznik rotacji
    last_max_timeout: int     # budżet ostatniego requestu (do recyklingu w tle)
    state: Literal["spawning", "idle", "busy", "recycling"]
```

Niezmienniki puli:

- `total = liczba slotów we wszystkich stanach` nigdy nie przekracza
  `MAX_BROWSERS` (o ile `MAX_BROWSERS != -1`).
- „Wolny do obsługi" = slot w stanie `idle` (browser **gotowy**), nie sam fakt
  zdjęcia locka. Slot `recycling`/`spawning` **nie** liczy się jako dostępny.
- Po każdej zmianie stanu wołamy `top_up_warm()`: dospawnuj idle aż
  `idle == min(WARM_BROWSERS, MAX_BROWSERS - (busy + spawning))`.

### Wybór/rezerwacja slotu (atomowo)

Operacja „znajdź lub utwórz slot" musi być atomowa względem współbieżnych
requestów. Krótkotrwały `asyncio.Lock` **tylko** na sekcję wyboru/rezerwacji
(nie na cały request):

```
async with _pool_lock:
    slot = pierwszy idle
    if slot: slot.state = busy; return slot
    if MAX_BROWSERS == -1 or total < MAX_BROWSERS:
        slot = nowy slot(state=spawning); zarejestruj; return slot  # spawn poza lockiem
    raise HTTPException(429, "Browser is busy processing another request")
```

Spawn (`_spawn_into(slot)`) wykonujemy **poza** lockiem (jest wolny), po nim
`slot.state = busy`.

### Cykl życia requestu (lock-then-respond)

1. `acquire_slot()` (wyżej) → `busy`, ewentualny spawn.
2. `_solve(request, slot)` — nawigacja, solver, budowa `LinkResponse`.
3. Zdejmij `busy`, ustaw stan końcowy:
   - błąd / `uses_left <= 0` → `recycling` + zaplanuj `recycle_slot(slot)` w tle
     (`asyncio.create_task`), z budżetem `last_max_timeout + MARGIN`.
   - inaczej → `idle` (browser zostaje, `uses_left -= 1`).
4. `top_up_warm()` (też w tle, nie blokuje odpowiedzi).
5. **Zwróć odpowiedź.** Recykling/top-up trwają niezależnie.

`recycle_slot`: `shutdown` przeglądarki + `rmtree(profile_dir)`; jeśli warm tego
wymaga — od razu respawn w to samo miejsce (`spawning → idle`), inaczej usuń
slot z puli. Całość pod `asyncio.wait_for(..., last_max_timeout + MARGIN)`;
timeout/wyjątek → `force_reset` slotu (wyzeruj uchwyty + `rmtree`) i usuń z puli.

---

## Warm pool — przykład `MAX_BROWSERS=3`, `WARM_BROWSERS=2`

| Zdarzenie | idle | busy | total | akcja |
|---|---|---|---|---|
| start | 2 | 0 | 2 | pre-spawn 2 warm |
| req#1 bierze slot | 1 | 1 | 2 | deficyt warm, total 3 ≤ MAX → spawn 1 warm |
| po dospawnowaniu | 2 | 1 | 3 | pełno |
| req#2 bierze slot | 1 | 2 | 3 | total=MAX → brak dospawnu |
| req#3 bierze slot | 0 | 3 | 3 | wszyscy zajęci |
| req#4 | 0 | 3 | 3 | brak idle + total=MAX → **429** |
| req#1 kończy (idle) | 1 | 2 | 3 | top-up: idle<WARM ale total=MAX → czeka |
| req#2 kończy (recycle) | 1 | 1 | 2→3 | recycle + respawn warm → 2 idle |

Wartości brzegowe:
- `WARM_BROWSERS = 0` → leniwe, zero idle-RAM (dzisiejsze zachowanie).
- `WARM_BROWSERS = 1` → jeden ciepły browser na następny request.
- `WARM_BROWSERS = MAX_BROWSERS` → pełna ciepła pula (najszybsze, stały RAM).
- Clamp: `WARM_BROWSERS = min(WARM_BROWSERS, MAX_BROWSERS)` (gdy `MAX != -1`).
- `ROTATE_EVERY = 0` (nigdy nie rotuj) → browsery nie giną, warm-respawn to
  no-op; pre-spawn nadal może utworzyć `WARM_BROWSERS` instancji na starcie.

---

## Zmienne środowiskowe (po zmianach)

| Zmienna | Default | Znaczenie |
|---|---|---|
| `FINGERPRINT_ROTATE_EVERY` | `1` | requestów na browser przed rotacją (wspólne) |
| `FINGERPRINT_CLEAR_BETWEEN` | **`false`** (zmiana) | czyść cookies/permissions między reużyciami |
| `MAX_BROWSERS` *(nowa)* | `1` | rozmiar puli; `-1` = bez limitu (= upstream) |
| `WARM_BROWSERS` *(nowa)* | `0` | ile bezczynnych browserów trzymać gotowych |
| `MAX_TIMEOUT_CAP` *(nowa)* | `60` | górny clamp dla `max_timeout` requestu (s) |
| `BROWSER_SHUTDOWN_MARGIN` *(nowa, zastępuje `BROWSER_SHUTDOWN_TIMEOUT`)* | `10` | margines doliczany do budżetu teardownu (s) |

---

## Kroki implementacji

1. **consts.py** — dodać `MAX_BROWSERS`, `WARM_BROWSERS`, `MAX_TIMEOUT_CAP`,
   `BROWSER_SHUTDOWN_MARGIN`; usunąć `BROWSER_SHUTDOWN_TIMEOUT`; zmienić default
   `FINGERPRINT_CLEAR_BETWEEN` na `false`. Walidacja/clamp `WARM ≤ MAX`.
2. **utils.py** — wprowadzić `BrowserSlot`, `_BrowserPool` (lista slotów +
   `_pool_lock`), funkcje: `acquire_slot`, `release_slot`, `recycle_slot`,
   `top_up_warm`, `prewarm`, `shutdown_pool`. Zachować sweep startowy i per-slot
   `rmtree`. `camoufox_session` przepisany na slot z lock-then-respond.
3. **endpoints.py** — `_solve` przyjmuje `BrowserSlot`/`CamoufoxDepClass` bez
   zmiany kontraktu `/v1`. Clamp `max_timeout` do `MAX_TIMEOUT_CAP` przy budowie
   `TimeoutTimer`. Zapisać `slot.last_max_timeout`.
4. **main.py** — `prewarm()` na starcie (zamiast/obok health-check),
   `shutdown_pool()` na zdarzeniu shutdown i w `init()`.
5. **Dokumentacja** — zaktualizować [FINGERPRINT_ENV.md](FINGERPRINT_ENV.md) o
   `MAX_BROWSERS`, `WARM_BROWSERS`, `MAX_TIMEOUT_CAP`, `BROWSER_SHUTDOWN_MARGIN`
   i nowy default `FINGERPRINT_CLEAR_BETWEEN`.
6. **Weryfikacja w Dockerze** (WSL): sekwencja bez cooldownu (brak 429),
   współbieżność do `MAX_BROWSERS` (powyżej → 429), warm pool utrzymuje idle,
   brak wycieku profili, recykling w tle nie blokuje odpowiedzi.

---

## Ryzyka / uwagi

- **`MAX_BROWSERS = -1`** odtwarza nielimitowaną współbieżność upstreamu →
  ryzyko OOM przy burstcie. Legalna opcja, ale z notką w docu.
- **Warm + `-1`:** top-up bez capu jest niebezpieczny; przy `MAX_BROWSERS = -1`
  traktować `WARM_BROWSERS` tylko jako liczbę pre-spawnu na starcie, bez
  agresywnego odbudowywania.
- **Atomowość puli:** cała mutacja stanu slotów wyłącznie pod `_pool_lock`;
  spawny/teardowny (długie) wykonywane poza lockiem na podstawie zarezerwowanego
  slotu.
- **Recykling w tle a shutdown kontenera:** `shutdown_pool()` musi poczekać/
  anulować zadania recyklingu i posprzątać profile, żeby nie zostawić sierot.
- **Złożoność:** to największa zmiana architektury w projekcie — wdrożyć w jednym
  kroku z testami stanów slotu (spawning/idle/busy/recycling).

---

## Kolejność wdrożenia

1. Tanie, niezależne: default `FINGERPRINT_CLEAR_BETWEEN = false` + clamp
   `MAX_TIMEOUT_CAP` + `BROWSER_SHUTDOWN_MARGIN` (zastąpienie stałej).
2. Model puli + slot (`MAX_BROWSERS`), bez warm (warm-deficyt = 0).
3. Warm pool (`WARM_BROWSERS`) + lock-then-respond (recykling w tle).
4. Aktualizacja dokumentacji + weryfikacja w Dockerze.
