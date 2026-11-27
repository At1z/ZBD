# Scenariusze testowe

32 scenariuszy wykonywanych w tej samej kolejności na każdej bazie (PostgreSQL, MySQL, MongoDB, Redis).
Ten plik jest generowany z `zbd/scenarios.py` (`uv run python -m zbd.scenarios --markdown`), nie edytuj go ręcznie.

- **Run** to jeden pełny przebieg S01–S32 dla jednego rozmiaru danych. Każdy rozmiar (100 000, 1 000 000,
  10 000 000 transakcji) jest powtarzany 3 razy, razem 9 runów na bazę.
- Kolejność jest stała, bo scenariusze zależą od siebie: S01 ładuje dane, S10–S13 dokładają nowe rekordy,
  S15 tworzy tabelę zagregowaną, S16–S21 zmieniają dane (S22–S23 pokazują skutek), S24–S28 usuwają
  (S29–S31 pokazują skutek), S32 sprząta.
- Parametry (id, filtry, nowe rekordy) są liczone deterministycznie z danych (`Params` w `zbd/scenarios.py`),
  więc każda baza dostaje dokładnie to samo. Wyniki scenariuszy są porównywane między bazami.
- Podział: 8 łatwych, 8 średnich, 16 trudnych. Typy: 6 × CREATE, 14 × READ, 6 × UPDATE, 6 × DELETE.
- Zapytania poniżej są w kanonicznym SQL (Postgres); `:nazwa` to parametr z `Params`.
  Warianty MongoDB i Redis opisują, jak to samo jest zrobione w tych bazach.

| # | Nazwa | Operacja | Trudność |
|---|---|---|---|
| 1 | Masowe ładowanie danych | CREATE | trudny |
| 2 | Odczyt po kluczu głównym (×1000) | READ | łatwy |
| 3 | Przychód per region (kanał Online) | READ | łatwy |
| 4 | Liczba transakcji z jednego miesiąca | READ | łatwy |
| 5 | Przychód per kategoria (JOIN 2 tabel) | READ | średni |
| 6 | Top 10 produktów wg przychodu | READ | średni |
| 7 | Zakupy 200 klientów z nazwami produktów | READ | średni |
| 8 | Segment × kategoria × region (JOIN 3 tabel) | READ | trudny |
| 9 | Miesięczny przychód per kategoria i grupa wiekowa | READ | trudny |
| 10 | Pojedyncze INSERT-y (×1000) | CREATE | łatwy |
| 11 | Wstawienie paczki 10 000 transakcji | CREATE | średni |
| 12 | Masowe wstawienie 100 000 transakcji | CREATE | trudny |
| 13 | Nowi klienci wraz z transakcjami (jedna transakcja) | CREATE | trudny |
| 14 | Odczyt wstawionych rekordów po kluczu (×1000) | READ | łatwy |
| 15 | Tabela zagregowana (CREATE TABLE AS, JOIN 3 tabel) | CREATE | trudny |
| 16 | Update po kluczu (×1000) | UPDATE | łatwy |
| 17 | Update po filtrze (region + miesiąc) | UPDATE | średni |
| 18 | Masowy update co setnej transakcji | UPDATE | trudny |
| 19 | Update całej tabeli z warunkiem (2 pola, pełny skan) | UPDATE | trudny |
| 20 | Update przez JOIN (wszystkie transakcje jednej kategorii) | UPDATE | trudny |
| 21 | Awans największych klientów (update z agregacją) | UPDATE | trudny |
| 22 | Przychód per kategoria po update'ach | READ | średni |
| 23 | Segment × kategoria × region po update'ach | READ | trudny |
| 24 | Usunięcie po kluczu (×1000) | DELETE | łatwy |
| 25 | Usunięcie po filtrze (region + miesiąc) | DELETE | średni |
| 26 | Usunięcie ok. 50% najstarszych transakcji | DELETE | trudny |
| 27 | Usunięcie transakcji klientów `Top spender` (podzapytanie) | DELETE | trudny |
| 28 | Usunięcie klientów bez transakcji (anty-join) | DELETE | trudny |
| 29 | Liczba transakcji po usunięciach | READ | łatwy |
| 30 | Przychód per kategoria po usunięciach | READ | średni |
| 31 | Segment × kategoria × region po usunięciach | READ | trudny |
| 32 | Usunięcie wszystkich danych | DELETE | trudny |

## 1. Masowe ładowanie danych

`S01` · CREATE · trudny

**Co robi:** Ładuje `customers`, `products` i N transakcji jednym bulk loadem (razem z indeksami). Mierzony jest cały `inject()`.

**Query:**

```sql
COPY customers    (customer_id, ...) FROM STDIN;
COPY products     (product_id, ...)  FROM STDIN;
COPY transactions (transaction_id, ...) FROM STDIN;
CREATE INDEX ON transactions (customer_id);
CREATE INDEX ON transactions (transaction_date);
-- MySQL: wielowierszowy INSERT po 10 000 wierszy; indeks po dacie po załadowaniu, potem ANALYZE TABLE;
-- indeksy kluczy obcych (customer_id, product_id) InnoDB tworzy sam od początku
```

- **MongoDB:** `insert_many(ordered=False)` paczkami po 50 000 + 2 indeksy na `transactions` (`customer_id`, `transaction_date`). Każda transakcja ma wbudowane pola klienta (segment, grupa wiekowa) i produktu (kategoria, nazwa), więc dokumenty są większe.
- **Redis:** Pipeline: `HSET` + `SADD` / `ZADD` (3 indeksy na transakcję: klient, kanał, data).

## 2. Odczyt po kluczu głównym (×1000)

`S02` · READ · łatwy

**Co robi:** 1000 osobnych zapytań o transakcję po `transaction_id` (id równomiernie rozłożone po całym zbiorze). Wynik: liczba znalezionych i suma `sales_amount`.

**Query:**

```sql
SELECT * FROM transactions WHERE transaction_id = :id;   -- 1000 razy
```

- **MongoDB:** `find_one({_id: id})` w pętli.
- **Redis:** `HGETALL transaction:{id}` w pętli.

## 3. Przychód per region (kanał Online)

`S03` · READ · łatwy

**Co robi:** Jedna tabela: liczba transakcji i przychód per region dla kanału `Online`.

**Query:**

```sql
SELECT region, COUNT(*), SUM(sales_amount)
FROM transactions
WHERE sales_channel = 'Online'
GROUP BY region
ORDER BY region;
```

- **MongoDB:** `$match` + `$group` + `$sort`.
- **Redis:** Identyfikatory z `idx:tx:channel:Online` (`SSCAN`, paczkami) → skrypt Lua w serwerze: `HMGET` + `GROUP BY region`; klient scala paczki.

## 4. Liczba transakcji z jednego miesiąca

`S04` · READ · łatwy

**Co robi:** Zliczenie transakcji z zakresu dat jednego miesiąca (używa indeksu po dacie).

**Query:**

```sql
SELECT COUNT(*) FROM transactions
WHERE transaction_date >= :count_start AND transaction_date < :count_end;
```

- **MongoDB:** `count_documents` z filtrem po `transaction_date`.
- **Redis:** `ZCOUNT idx:tx:date min max`.

## 5. Przychód per kategoria (JOIN 2 tabel)

`S05` · READ · średni

**Co robi:** Transakcje połączone z produktami: liczba, przychód per kategoria.

**Query:**

```sql
SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.category
ORDER BY revenue DESC, p.category;
```

- **MongoDB:** `$group` po wbudowanej kategorii produktu (bez `$lookup`).
- **Redis:** Identyfikatory wszystkich transakcji (`ZRANGE idx:tx:date`, paczkami) → skrypt Lua w serwerze: `HMGET`, kategoria z `HGET product:{id}`, `GROUP BY`; klient scala paczki.

## 6. Top 10 produktów wg przychodu

`S06` · READ · średni

**Co robi:** Przychód per produkt z nazwą produktu, 10 najlepszych.

**Query:**

```sql
SELECT p.product_id, p.product_name, SUM(t.sales_amount) AS revenue
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.product_id, p.product_name
ORDER BY revenue DESC, p.product_id
LIMIT 10;
```

- **MongoDB:** `$group` po `product_id` (nazwa produktu wbudowana, `$first`) → `$sort` → `$limit`.
- **Redis:** Jak S05, ale skrypt grupuje po `product_id`; nazwy 10 najlepszych klient bierze ze słownika produktów.

## 7. Zakupy 200 klientów z nazwami produktów

`S07` · READ · średni

**Co robi:** Dla 200 klientów (równomiernie rozłożonych) pobiera ich transakcje z nazwami produktów (JOIN). Wynik: liczba wierszy i liczba różnych nazw produktów.

**Query:**

```sql
SELECT t.transaction_id, p.product_name
FROM transactions t
JOIN products p USING (product_id)
WHERE t.customer_id = :customer_id;   -- 200 razy
```

- **MongoDB:** `find` po `customer_id` (nazwa produktu wbudowana), w pętli.
- **Redis:** `SMEMBERS idx:tx:customer:{id}` → `HMGET product_id`, nazwa ze słownika produktów.

## 8. Segment × kategoria × region (JOIN 3 tabel)

`S08` · READ · trudny

**Co robi:** Zapytanie `heavy`: przychód per segment klienta, kategoria i region od 2025-01-01, grupy powyżej 1000, top 20.

**Query:**

```sql
SELECT c.customer_segment, p.category, t.region, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
FROM transactions t
JOIN customers c USING (customer_id)
JOIN products p USING (product_id)
WHERE t.transaction_date >= :heavy_since
GROUP BY c.customer_segment, p.category, t.region
HAVING SUM(t.sales_amount) > 1000
ORDER BY revenue DESC, c.customer_segment, p.category, t.region
LIMIT 20;
```

- **MongoDB:** `$match` po dacie + `$group` po wbudowanych polach (segment, kategoria) + `$match` + `$sort` + `$limit`, bez `$lookup`; wymuszony skan kolekcji (`hint $natural`), bo przy 10M planner z indeksem po dacie czytał dokumenty losowo i był wielokrotnie wolniejszy.
- **Redis:** Identyfikatory od `heavy_since` (`idx:tx:date`, paczkami) → skrypt Lua: `HMGET`, segment klienta (`HGET customer:{id}`) i kategoria (`HGET product:{id}`), `GROUP BY`; klient scala paczki i filtruje `HAVING`.

## 9. Miesięczny przychód per kategoria i grupa wiekowa

`S09` · READ · trudny

**Co robi:** JOIN 3 tabel, grupowanie po roku, miesiącu, kategorii i grupie wiekowej klienta; top 20 wg przychodu.

**Query:**

```sql
SELECT EXTRACT(YEAR FROM t.transaction_date) AS y, EXTRACT(MONTH FROM t.transaction_date) AS m,
       p.category, c.customer_age_group, SUM(t.sales_amount) AS revenue
FROM transactions t
JOIN customers c USING (customer_id)
JOIN products p USING (product_id)
GROUP BY y, m, p.category, c.customer_age_group
ORDER BY revenue DESC, y, m, p.category, c.customer_age_group
LIMIT 20;
```

- **MongoDB:** `$group` po `$year` / `$month` i wbudowanych polach (kategoria, grupa wiekowa) + `$sort` + `$limit`, bez `$lookup`.
- **Redis:** Identyfikatory wszystkich transakcji (paczkami) → skrypt Lua: rok i miesiąc z daty, kategoria i grupa wiekowa przez `HGET`, `GROUP BY`; klient scala paczki.

## 10. Pojedyncze INSERT-y (×1000)

`S10` · CREATE · łatwy

**Co robi:** 1000 nowych transakcji wstawianych pojedynczo (osobne polecenie na wiersz, jeden commit na końcu). Nowe transakcje wskazują na istniejących klientów i produkty.

**Query:**

```sql
INSERT INTO transactions (transaction_id, ..., region) VALUES (...);   -- 1000 razy
```

- **MongoDB:** `insert_one` w pętli (wcześniej odczyt pól klienta i produktu, które trzeba wbudować).
- **Redis:** `HSET` + `SADD` / `ZADD` w pętli (wszystkie indeksy).

## 11. Wstawienie paczki 10 000 transakcji

`S11` · CREATE · średni

**Co robi:** 10 000 kolejnych nowych transakcji wstawionych paczkami.

**Query:**

```sql
INSERT INTO transactions (transaction_id, ..., region) VALUES (...), (...), ...;   -- paczki, razem 10 000
```

- **MongoDB:** `insert_many` paczkami (wcześniej odczyt pól klienta i produktu do wbudowania).
- **Redis:** Pipeline `HSET` + `SADD` / `ZADD`.

## 12. Masowe wstawienie 100 000 transakcji

`S12` · CREATE · trudny

**Co robi:** 100 000 kolejnych nowych transakcji (istniejący klienci i produkty) wstawionych paczkami, jeden commit na końcu.

**Query:**

```sql
INSERT INTO transactions (transaction_id, ..., region) VALUES (...), (...), ...;   -- paczki, razem 100 000
```

- **MongoDB:** `insert_many` paczkami po 5000 (wcześniej odczyt pól klienta i produktu do wbudowania).
- **Redis:** Pipeline `HSET` + `SADD` / `ZADD` paczkami po 10 000.

## 13. Nowi klienci wraz z transakcjami (jedna transakcja)

`S13` · CREATE · trudny

**Co robi:** 5000 nowych klientów i po 5 transakcji każdego z nich (25 000), zapisane razem: klienci muszą powstać przed transakcjami, które na nich wskazują (klucz obcy). Wynik: liczba wstawionych wierszy (klienci + transakcje).

**Query:**

```sql
INSERT INTO customers (customer_id, ...) VALUES (...), ...;      -- 5000 klientów
INSERT INTO transactions (transaction_id, ...) VALUES (...), ...; -- 25 000 transakcji
COMMIT;   -- jedna transakcja na całość
```

- **MongoDB:** `insert_many` klientów, potem `insert_many` transakcji z wbudowanymi polami klienta i produktu (bez wspólnej transakcji).
- **Redis:** Pipeline: `HSET customer:{id}` + `SADD ids:customers`, potem `HSET` + `SADD` / `ZADD` transakcji (bez wspólnej transakcji).

## 14. Odczyt wstawionych rekordów po kluczu (×1000)

`S14` · READ · łatwy

**Co robi:** Odczyt po kluczu 1000 transakcji wstawionych w S10 (sprawdza, że insert jest widoczny). Wynik: liczba znalezionych i suma `sales_amount`.

**Query:**

```sql
SELECT * FROM transactions WHERE transaction_id = :new_id;   -- 1000 razy
```

- **MongoDB:** `find_one({_id: id})` w pętli.
- **Redis:** `HGETALL transaction:{id}` w pętli.

## 15. Tabela zagregowana (CREATE TABLE AS, JOIN 3 tabel)

`S15` · CREATE · trudny

**Co robi:** Tworzy nową tabelę `monthly_sales` z wynikiem zapytania: rok × miesiąc × kategoria × grupa wiekowa (liczba transakcji i przychód), bez `LIMIT`. Wynik: liczba wierszy nowej tabeli. Tabela (kolekcja, klucze) jest usuwana w S32.

**Query:**

```sql
CREATE TABLE monthly_sales AS
SELECT EXTRACT(YEAR FROM t.transaction_date) AS y, EXTRACT(MONTH FROM t.transaction_date) AS m,
       p.category, c.customer_age_group, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
FROM transactions t
JOIN customers c USING (customer_id)
JOIN products p USING (product_id)
GROUP BY 1, 2, 3, 4;
```

- **MongoDB:** `$group` po `$year` / `$month` i wbudowanych polach + `$out` do kolekcji `monthly_sales`.
- **Redis:** Jak S09 bez limitu (skrypt Lua w serwerze + scalanie w kliencie), potem `HSET monthly_sales:{...}` dla każdej grupy.

## 16. Update po kluczu (×1000)

`S16` · UPDATE · łatwy

**Co robi:** `quantity + 1` dla 1000 transakcji, osobne polecenie na każdą (jeden commit na końcu).

**Query:**

```sql
UPDATE transactions SET quantity = quantity + 1 WHERE transaction_id = :id;   -- 1000 razy
```

- **MongoDB:** `update_one` z `$inc` w pętli.
- **Redis:** `HINCRBY transaction:{id} quantity 1` tylko dla istniejących kluczy (skrypt Lua).

## 17. Update po filtrze (region + miesiąc)

`S17` · UPDATE · średni

**Co robi:** `discount_pct + 1` dla wszystkich transakcji z jednego regionu w jednym miesiącu (jedno polecenie).

**Query:**

```sql
UPDATE transactions SET discount_pct = discount_pct + 1
WHERE region = :region AND transaction_date >= :update_start AND transaction_date < :update_end;
```

- **MongoDB:** `update_many` z `$inc`.
- **Redis:** `ZRANGEBYSCORE idx:tx:date` → filtr po regionie → `HINCRBY discount_pct`.

## 18. Masowy update co setnej transakcji

`S18` · UPDATE · trudny

**Co robi:** `quantity + 1` dla co 100. transakcji (1% zbioru) paczkami po 1000 id.

**Query:**

```sql
UPDATE transactions SET quantity = quantity + 1
WHERE transaction_id IN (:id_1, ..., :id_1000);   -- paczki
```

- **MongoDB:** `update_many({_id: {$in: [...]}}, {$inc: {quantity: 1}})` paczkami.
- **Redis:** Pipeline `HINCRBY` (skrypt Lua) paczkami po 10 000.

## 19. Update całej tabeli z warunkiem (2 pola, pełny skan)

`S19` · UPDATE · trudny

**Co robi:** `quantity + 1` i `discount_pct + 1` dla wszystkich transakcji z `discount_pct` nie mniejszym niż mediana (pole bez indeksu, więc każda baza czyta cały zbiór), jednym poleceniem.

**Query:**

```sql
UPDATE transactions SET quantity = quantity + 1, discount_pct = discount_pct + 1
WHERE discount_pct >= :discount_min;
```

- **MongoDB:** `update_many` z `$inc` na dwóch polach.
- **Redis:** Identyfikatory wszystkich transakcji (paczkami po 50 000) → skrypt Lua: `HGET discount_pct`, `HINCRBY` dwóch pól dla pasujących.

## 20. Update przez JOIN (wszystkie transakcje jednej kategorii)

`S20` · UPDATE · trudny

**Co robi:** `discount_pct + 2` dla wszystkich transakcji produktów z jednej kategorii: kategoria jest w tabeli `products`, więc update łączy dwie tabele.

**Query:**

```sql
UPDATE transactions t SET discount_pct = t.discount_pct + 2
FROM products p
WHERE p.product_id = t.product_id AND p.category = :update_category;
-- MySQL: UPDATE transactions t JOIN products p ON ... SET ... WHERE p.category = ...
```

- **MongoDB:** `update_many({"product.category": ...}, {$inc: ...})` na wbudowanym polu (bez joina).
- **Redis:** Identyfikatory produktów danej kategorii → skrypt Lua po wszystkich transakcjach (paczkami po 50 000): `HGET product_id`, `HINCRBY` dla pasujących.

## 21. Awans największych klientów (update z agregacją)

`S21` · UPDATE · trudny

**Co robi:** Klienci, których łączny przychód jest powyżej 80. percentyla, dostają segment `Top spender`. Agregacja po całej tabeli `transactions` steruje update'em `customers`; skutek widać w S23, S27 i S31. Wynik: liczba zmienionych klientów.

**Query:**

```sql
UPDATE customers SET customer_segment = 'Top spender'
WHERE customer_id IN (
    SELECT customer_id FROM transactions
    GROUP BY customer_id HAVING SUM(sales_amount) > :vip_threshold
);
```

- **MongoDB:** `$group` po `customer_id` + `$match` → paczkami po 1000 id: `update_many` w `customers` oraz `update_many` wbudowanego `customer.segment` w `transactions` (po indeksie `customer_id`).
- **Redis:** Identyfikatory klientów (`SSCAN ids:customers`, paczkami) → skrypt Lua: suma `sales_amount` z `idx:tx:customer:{id}`, `HSET customer_segment` dla przekraczających próg.

## 22. Przychód per kategoria po update'ach

`S22` · READ · średni

**Co robi:** To samo co S05, ale dodatkowo `SUM(quantity)`: widać skutek update'ów S16–S21.

**Query:**

```sql
SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.category
ORDER BY revenue DESC, p.category;
```

- **MongoDB:** Jak S05 + `$sum: "$quantity"`.
- **Redis:** Jak S05 + suma `quantity`.

## 23. Segment × kategoria × region po update'ach

`S23` · READ · trudny

**Co robi:** To samo co S08, ale dodatkowo `SUM(quantity)`.

**Query:**

```sql
SELECT c.customer_segment, p.category, t.region, COUNT(*) AS n,
       SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN customers c USING (customer_id)
JOIN products p USING (product_id)
WHERE t.transaction_date >= :heavy_since
GROUP BY c.customer_segment, p.category, t.region
HAVING SUM(t.sales_amount) > 1000
ORDER BY revenue DESC, c.customer_segment, p.category, t.region
LIMIT 20;
```

- **MongoDB:** Jak S08 + `$sum: "$quantity"`.
- **Redis:** Jak S08 + suma `quantity`.

## 24. Usunięcie po kluczu (×1000)

`S24` · DELETE · łatwy

**Co robi:** Usuwa 1000 transakcji wstawionych w S10, po jednej.

**Query:**

```sql
DELETE FROM transactions WHERE transaction_id = :new_id;   -- 1000 razy
```

- **MongoDB:** `delete_one` w pętli.
- **Redis:** `DEL` + `SREM` / `ZREM` ze wszystkich indeksów, w pętli.

## 25. Usunięcie po filtrze (region + miesiąc)

`S25` · DELETE · średni

**Co robi:** Usuwa transakcje z jednego regionu z jednego miesiąca (inny miesiąc niż w S17), jednym poleceniem.

**Query:**

```sql
DELETE FROM transactions
WHERE region = :region AND transaction_date >= :delete_start AND transaction_date < :delete_end;
```

- **MongoDB:** `delete_many` z filtrem.
- **Redis:** `ZRANGEBYSCORE idx:tx:date` → filtr po regionie → `DEL` + `SREM` / `ZREM` z indeksów.

## 26. Usunięcie ok. 50% najstarszych transakcji

`S26` · DELETE · trudny

**Co robi:** Usuwa wszystkie transakcje starsze niż mediana dat.

**Query:**

```sql
DELETE FROM transactions WHERE transaction_date < :cutoff_date;
```

- **MongoDB:** `delete_many({transaction_date: {$lt: cutoff}})` z `hint $natural` (skan kolekcji zamiast indeksu po dacie); gdyby serwer odrzucił hint przy usuwaniu, wykonuje się zwykłe `delete_many`.
- **Redis:** `ZRANGEBYSCORE idx:tx:date -inf (cutoff` → `DEL` + `SREM` / `ZREM` z indeksów, paczkami.

## 27. Usunięcie transakcji klientów `Top spender` (podzapytanie)

`S27` · DELETE · trudny

**Co robi:** Usuwa wszystkie transakcje klientów, którzy w S21 dostali segment `Top spender` (segment jest w tabeli `customers`).

**Query:**

```sql
DELETE FROM transactions
WHERE customer_id IN (SELECT customer_id FROM customers WHERE customer_segment = 'Top spender');
```

- **MongoDB:** `delete_many({"customer.segment": "Top spender"})` na wbudowanym polu (skan kolekcji).
- **Redis:** `HGET customer_segment` po `ids:customers` → dla każdego takiego klienta `SMEMBERS idx:tx:customer:{id}` → `DEL` + `SREM` / `ZREM` z indeksów, paczkami.

## 28. Usunięcie klientów bez transakcji (anty-join)

`S28` · DELETE · trudny

**Co robi:** Usuwa z `customers` klientów, którzy po S24–S27 nie mają już żadnej transakcji (m.in. wszystkich `Top spender`). Wynik: liczba usuniętych klientów.

**Query:**

```sql
DELETE FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.customer_id = c.customer_id);
```

- **MongoDB:** `$lookup` z `$limit: 1` (po indeksie `customer_id`) + `$match` pustych wyników → `delete_many({_id: {$in: [...]}})` paczkami.
- **Redis:** `SCARD idx:tx:customer:{id}` dla każdego z `ids:customers` → `DEL customer:{id}` + `SREM ids:customers` dla pustych.

## 29. Liczba transakcji po usunięciach

`S29` · READ · łatwy

**Co robi:** Zlicza pozostałe transakcje.

**Query:**

```sql
SELECT COUNT(*) FROM transactions;
```

- **MongoDB:** `count_documents({})`.
- **Redis:** `ZCARD idx:tx:date`.

## 30. Przychód per kategoria po usunięciach

`S30` · READ · średni

**Co robi:** To samo co S22, na danych po S24–S28.

**Query:**

```sql
SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.category
ORDER BY revenue DESC, p.category;
```

- **MongoDB:** Jak S22.
- **Redis:** Jak S22.

## 31. Segment × kategoria × region po usunięciach

`S31` · READ · trudny

**Co robi:** To samo co S23, na danych po S24–S28.

**Query:**

```sql
SELECT c.customer_segment, p.category, t.region, COUNT(*) AS n,
       SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN customers c USING (customer_id)
JOIN products p USING (product_id)
WHERE t.transaction_date >= :heavy_since
GROUP BY c.customer_segment, p.category, t.region
HAVING SUM(t.sales_amount) > 1000
ORDER BY revenue DESC, c.customer_segment, p.category, t.region
LIMIT 20;
```

- **MongoDB:** Jak S23.
- **Redis:** Jak S23.

## 32. Usunięcie wszystkich danych

`S32` · DELETE · trudny

**Co robi:** Usuwa wszystkie dane ze wszystkich trzech tabel oraz tabelę `monthly_sales` z S15. Wynik: liczba pozostałych wierszy (musi być 0).

**Query:**

```sql
DELETE FROM transactions;
DELETE FROM products;
DELETE FROM customers;
DROP TABLE monthly_sales;
```

- **MongoDB:** `delete_many({})` na trzech kolekcjach + `drop` kolekcji `monthly_sales`.
- **Redis:** `FLUSHDB` (czas nieporównywalny z `DELETE` w SQL).
