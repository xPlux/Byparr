# Byparr — nowe zmienne środowiskowe (shared browser)

Te dwie zmienne sterują współdzieleniem instancji Camoufox między żądaniami. Domyślne wartości zachowują poprzednie zachowanie (restart na każdy request).

## `FINGERPRINT_ROTATE_EVERY`

- Typ: `int`
- Default: `1`
- Znaczenie: liczba żądań obsłużonych przez tę samą instancję Camoufox (i ten sam fingerprint) zanim zostanie zrestartowana.
  - `1` — legacy: nowa instancja na każdy request.
  - `N > 1` — instancja jest reużywana przez N requestów, potem teardown + nowy fingerprint.
  - `0` lub ujemne — instancja nigdy nie jest rotowana (żyje do shutdownu kontenera).
- Uwaga: każdy request liczy się do licznika, również nieudany (failed request i tak wymusza teardown).

## `FINGERPRINT_CLEAR_BETWEEN`

- Typ: `bool` (`1`/`true`/`yes`/`on` = true; cokolwiek innego = false)
- Default: `true`
- Znaczenie: gdy instancja jest reużywana (między requestami w obrębie tego samego fingerprintu), przed nowym requestem:
  - `clear_cookies()` na kontekście,
  - `clear_permissions()` na kontekście,
  - zamknięcie poprzedniej strony i otwarcie świeżej.
- Wyłącz (`false`) jeśli świadomie chcesz utrzymywać sesję/ciasteczka między requestami.

## Concurrency

Brak locka. Drugi równoległy request dostaje natychmiast `HTTP 429 {"detail": "Browser is busy processing another request"}`. Klient powinien zrobić retry z backoffem.

## Przykład — docker compose

```yaml
services:
  byparr:
    image: ghcr.io/xplux/byparr:main
    ports:
      - "8191:8191"
    environment:
      FINGERPRINT_ROTATE_EVERY: "20"
      FINGERPRINT_CLEAR_BETWEEN: "true"
```

## Przykład — docker run

```bash
docker run -d --name byparr -p 8191:8191 \
  -e FINGERPRINT_ROTATE_EVERY=20 \
  -e FINGERPRINT_CLEAR_BETWEEN=true \
  ghcr.io/xplux/byparr:main
```
