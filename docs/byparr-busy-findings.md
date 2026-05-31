# Analiza: „Browser is busy processing another request"

Odpowiedź na pytania z [byparr-busy-investigation.md](byparr-busy-investigation.md), zweryfikowana wprost na kodzie ([src/utils.py](../src/utils.py), [src/endpoints.py](../src/endpoints.py), [src/models.py](../src/models.py), [Dockerfile](../Dockerfile)).

## TL;DR

Są **dwie** niezależne przyczyny utrwalonego „busy":

1. **Najważniejsza — niezgodność jednostek `maxTimeout`.** Byparr traktuje `maxTimeout` jako **sekundy**, a wy wysyłacie wartość w **milisekundach** (FlareSolverr-style). `maxTimeout: 25000` to dla byparr **25 000 sekund (~6,9 h)** budżetu, więc wewnętrzne timeouty praktycznie nigdy nie odpalają. Zawieszona nawigacja trzyma flagę `busy` aż do restartu kontenera. To wprost tłumaczy obserwowany objaw.
2. **Wtórna — teardown bez własnego timeoutu.** Nawet przy poprawnym czasie, zwolnienie flagi `busy` następuje dopiero **po** zamknięciu przeglądarki. Jeśli zawieszony Firefox nie chce się domknąć, `shutdown_shared_browser()` może wisieć, a `busy` nigdy nie wróci do `False`.

## Model współbieżności (gdzie jest „lock")

- Jest **jedna współdzielona instancja** Camoufox na kontener, chroniona flagą `_shared.busy` w `get_camoufox()` ([src/utils.py](../src/utils.py)).
- Wejście do zależności: jeśli `busy` jest `True` → natychmiast `HTTP 429 {"detail":"Browser is busy processing another request"}`. To **immediate-reject**, nie kolejka.
- `busy` ustawiane na `True` na wejściu, czyszczone w `finally` (po ewentualnym teardownie). Czyli to nie jest klasyczny `asyncio.Lock`, tylko flaga zajętości.

```python
if _shared.busy:
    raise HTTPException(429, "Browser is busy processing another request")
_shared.busy = True
try:
    ...
    yield CamoufoxDepClass(...)
finally:
    try:
        if request_failed: await shutdown_shared_browser()
        elif FINGERPRINT_ROTATE_EVERY > 0: ... # rotacja
    finally:
        _shared.busy = False
```

## Czy `maxTimeout` faktycznie przerywa nawigację?

**Tak — co do mechaniki, ale tylko jeśli wartość jest w sekundach.** W [src/endpoints.py](../src/endpoints.py) cała ścieżka jest spięta wspólnym budżetem `TimeoutTimer(duration=request.max_timeout)`:

- `page.goto(..., timeout=timer.remaining()*1000)`
- `wait_for_load_state(..., timeout=timer.remaining()*1000)` (×2)
- `wait_for(solver.solve_captcha(...), timeout=timer.remaining())`

Po przekroczeniu leci `TimeoutError → HTTP 408`, wyjątek wychodzi przez generator zależności, odpala się `finally`, a w nim `shutdown_shared_browser()` i `busy=False`. **Czyli przy poprawnej jednostce lock JEST zwalniany na timeout.**

Problem: `request.max_timeout` jest zdefiniowany jako sekundy:

```python
# src/models.py
max_timeout: int = Field(default=60, description="Maximum timeout in seconds ...")
```

a `TimeoutTimer.remaining()` liczy w sekundach. Wysyłając `25000`, dajecie ~6,9 h, więc `timer.remaining()` praktycznie nigdy nie schodzi do zera w realnym czasie życia requestu — timeout nie strzela, request wisi, `busy` trzyma.

## Obsługa rozłączenia klienta (curl po 55 s)

Gdy klient się rozłącza, Starlette anuluje task handlera → do `yield` wpada `CancelledError` → `request_failed=True` → `shutdown_shared_browser()` w `finally`. **W teorii lock się zwalnia.** W praktyce zależy to od tego, czy zamknięcie zawieszonej przeglądarki się zakończy (patrz niżej) — jeśli `close()`/`__aexit__` wiszą, anulacja nie pomoże i `busy` zostaje.

## Recovery / watchdog

**Brak.** Nie ma żadnego watchdoga, który force-zabiłby zawieszony kontekst i zresetował `busy`. Jedyne ścieżki zwolnienia to normalne zakończenie requestu albo teardown w `finally`. Jeśli teardown wisi, kontener jest zawieszony do restartu — a `HEALTHCHECK` ([Dockerfile](../Dockerfile)) bije w `/health`, które **samo wymaga wolnej przeglądarki**, więc przy zawieszeniu healthcheck zacznie failować dopiero po swoim własnym timeoucie (15 min interwał) — nie raportuje realnego stanu busy/idle natychmiast.

## Kolejka requestów?

Nie ma. Jest immediate-reject-on-busy (429). Żeby to zmienić, trzeba albo (a) dodać kolejkę/`asyncio.Lock` z limitem oczekiwania, albo (b) twardo gwarantować zwolnienie przeglądarki na timeout/disconnect (rekomendowane — prościej i bezpieczniej).

## Rekomendowane poprawki

W kolejności ważności:

1. **Naprawić jednostkę czasu (przyczyna #1).** Albo po waszej stronie wysyłać sekundy (`maxTimeout: 25` zamiast `25000`), albo w byparr przyjmować milisekundy zgodnie z kontraktem FlareSolverr:
   ```python
   # endpoints.py – konwersja ms -> s przy budowie timera
   timer = TimeoutTimer(duration=request.max_timeout / 1000)
   ```
   (uwaga: zmiana semantyki pola — wymaga uzgodnienia ze wszystkimi callerami; bezpieczniej najpierw poprawić caller).

2. **Twardy timeout na teardown (przyczyna #2).** Owinąć zamykanie w `asyncio.wait_for`, żeby `busy` zawsze wracało do `False`:
   ```python
   try:
       await asyncio.wait_for(shutdown_shared_browser(), timeout=15)
   except (TimeoutError, Exception):
       _force_reset_shared()  # wyzeruj uchwyty + rmtree profilu bez czekania
   ```
   Dzięki temu zawieszony `close()` Firefoksa nie zablokuje flagi na stałe.

3. **Globalny watchdog na request.** Owinąć cały `read_item` w `asyncio.wait_for(..., timeout=max_timeout + margines)`, niezależnie od wewnętrznych timeoutów Playwrighta, jako siatka bezpieczeństwa.

4. **`/health` raportujące realny stan.** Lekki endpoint zwracający `{"busy": _shared.busy}` bez odpalania przeglądarki, żeby healthcheck/caller wykrywał zawieszony kontener bez zużywania instancji.

## Co zostaje bez zmian

Kontrakt `/v1` (`status`, `solution.response`) nie wymaga zmian dla poprawek #2–#4. Poprawka #1 dotyka tylko interpretacji `maxTimeout` — najlepiej zacząć od strony wywołującej.
