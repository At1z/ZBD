# Scenariusze testowe

24 scenariusze wykonywane w tej samej kolejności na każdej bazie (PostgreSQL, MySQL, MongoDB, Redis).
Ten plik jest generowany z `scenarios.py` (`uv run scenarios.py --markdown`), nie edytuj go ręcznie.

- **Run** to jeden pełny przebieg S01–S24 dla jednego rozmiaru danych. Każdy rozmiar (100 000, 1 000 000,
  10 000 000 transakcji) jest powtarzany 3 razy, razem 9 runów na bazę.
- Kolejność jest stała, bo scenariusze zależą od siebie: S01 ładuje dane, S10–S11 dokładają nowe rekordy,
  S13–S15 je zmieniają (S16–S17 pokazują skutek), S18–S20 usuwają (S21–S23 pokazują skutek), S24 sprząta.
- Parametry (id, filtry, nowe rekordy) są liczone deterministycznie z danych (`Params` w `scenarios.py`),
  więc każda baza dostaje dokładnie to samo. Wyniki scenariuszy są porównywane między bazami.
- Podział: 8 łatwych, 8 średnich, 8 trudnych. Typy: 3 × CREATE, 14 × READ, 3 × UPDATE, 4 × DELETE.
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
| 12 | Odczyt wstawionych rekordów po kluczu (×1000) | READ | łatwy |
| 13 | Update po kluczu (×1000) | UPDATE | łatwy |
| 14 | Update po filtrze (region + miesiąc) | UPDATE | średni |
| 15 | Masowy update co setnej transakcji | UPDATE | trudny |
| 16 | Przychód per kategoria po update'ach | READ | średni |
| 17 | Segment × kategoria × region po update'ach | READ | trudny |
| 18 | Usunięcie po kluczu (×1000) | DELETE | łatwy |
| 19 | Usunięcie po filtrze (region + miesiąc) | DELETE | średni |
| 20 | Usunięcie ok. 50% najstarszych transakcji | DELETE | trudny |
| 21 | Liczba transakcji po usunięciach | READ | łatwy |
| 22 | Przychód per kategoria po usunięciach | READ | średni |
| 23 | Segment × kategoria × region po usunięciach | READ | trudny |
| 24 | Usunięcie wszystkich danych | DELETE | trudny |

## 1. Masowe ładowanie danych

`S01` · CREATE · trudny

**Co robi:** Ładuje `customers`, `products` i N transakcji jednym bulk loadem (razem z indeksami). Mierzony jest cały `inject()`.

**Query:**

```sql
COPY customers    (customer_id, ...) FROM STDIN;
COPY products     (product_id, ...)  FROM STDIN;
COPY transactions (transaction_id, ...) FROM STDIN;
CREATE INDEX ON transactions (customer_id);
CREATE INDEX ON transactions (product_id);
CREATE INDEX ON transactions (transaction_date);
-- MySQL: wielowierszowy INSERT po 10 000 wierszy
```

- **MongoDB:** `insert_many(ordered=False)` paczkami po 50 000 + 3 indeksy na `transactions`.
- **Redis:** Pipeline: `HSET` + `SADD` / `ZADD` (5 indeksów na transakcję).

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
- **Redis:** `SMEMBERS idx:tx:channel:Online` → `HMGET region, sales_amount`, agregacja w kliencie.

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

- **MongoDB:** `$lookup` na `products` + `$unwind` + `$group`.
- **Redis:** Wszystkie transakcje (`ZRANGE idx:tx:date`) → `HMGET`; kategoria ze słownika produktów, agregacja w kliencie.

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

- **MongoDB:** `$group` po `product_id` → `$sort` → `$limit` → `$lookup` na `products`.
- **Redis:** Jak S05, ale grupowanie po `product_id` w kliencie.

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

- **MongoDB:** `aggregate`: `$match` po `customer_id` + `$lookup` na `products`, w pętli.
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

- **MongoDB:** `$match` po dacie + dwa `$lookup` + `$group` + `$match` + `$sort` + `$limit`.
- **Redis:** `ZRANGEBYSCORE idx:tx:date` → `HMGET`; segment klienta dociągany `HGET`, agregacja w kliencie.

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

- **MongoDB:** Dwa `$lookup` + `$group` po `$year` / `$month` + `$sort` + `$limit`.
- **Redis:** Pełny skan transakcji; rok i miesiąc z daty, kategoria i grupa wiekowa ze słowników; agregacja w kliencie.

## 10. Pojedyncze INSERT-y (×1000)

`S10` · CREATE · łatwy

**Co robi:** 1000 nowych transakcji wstawianych pojedynczo (osobne polecenie na wiersz, jeden commit na końcu). Nowe transakcje wskazują na istniejących klientów i produkty.

**Query:**

```sql
INSERT INTO transactions (transaction_id, ..., region) VALUES (...);   -- 1000 razy
```

- **MongoDB:** `insert_one` w pętli.
- **Redis:** `HSET` + `SADD` / `ZADD` w pętli (wszystkie indeksy).

## 11. Wstawienie paczki 10 000 transakcji

`S11` · CREATE · średni

**Co robi:** 10 000 kolejnych nowych transakcji wstawionych paczkami.

**Query:**

```sql
INSERT INTO transactions (transaction_id, ..., region) VALUES (...), (...), ...;   -- paczki, razem 10 000
```

- **MongoDB:** `insert_many` paczkami.
- **Redis:** Pipeline `HSET` + `SADD` / `ZADD`.

## 12. Odczyt wstawionych rekordów po kluczu (×1000)

`S12` · READ · łatwy

**Co robi:** Odczyt po kluczu 1000 transakcji wstawionych w S10 (sprawdza, że insert jest widoczny). Wynik: liczba znalezionych i suma `sales_amount`.

**Query:**

```sql
SELECT * FROM transactions WHERE transaction_id = :new_id;   -- 1000 razy
```

- **MongoDB:** `find_one({_id: id})` w pętli.
- **Redis:** `HGETALL transaction:{id}` w pętli.

## 13. Update po kluczu (×1000)

`S13` · UPDATE · łatwy

**Co robi:** `quantity + 1` dla 1000 transakcji, osobne polecenie na każdą (jeden commit na końcu).

**Query:**

```sql
UPDATE transactions SET quantity = quantity + 1 WHERE transaction_id = :id;   -- 1000 razy
```

- **MongoDB:** `update_one` z `$inc` w pętli.
- **Redis:** `HINCRBY transaction:{id} quantity 1` tylko dla istniejących kluczy (skrypt Lua).

## 14. Update po filtrze (region + miesiąc)

`S14` · UPDATE · średni

**Co robi:** `discount_pct + 1` dla wszystkich transakcji z jednego regionu w jednym miesiącu (jedno polecenie).

**Query:**

```sql
UPDATE transactions SET discount_pct = discount_pct + 1
WHERE region = :region AND transaction_date >= :update_start AND transaction_date < :update_end;
```

- **MongoDB:** `update_many` z `$inc`.
- **Redis:** `ZRANGEBYSCORE idx:tx:date` → filtr po regionie → `HINCRBY discount_pct`.

## 15. Masowy update co setnej transakcji

`S15` · UPDATE · trudny

**Co robi:** `quantity + 1` dla co 100. transakcji (1% zbioru) paczkami po 1000 id.

**Query:**

```sql
UPDATE transactions SET quantity = quantity + 1
WHERE transaction_id IN (:id_1, ..., :id_1000);   -- paczki
```

- **MongoDB:** `update_many({_id: {$in: [...]}}, {$inc: {quantity: 1}})` paczkami.
- **Redis:** Pipeline `HINCRBY` (skrypt Lua) paczkami po 10 000.

## 16. Przychód per kategoria po update'ach

`S16` · READ · średni

**Co robi:** To samo co S05, ale dodatkowo `SUM(quantity)`: widać skutek update'ów S13–S15.

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

## 17. Segment × kategoria × region po update'ach

`S17` · READ · trudny

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

## 18. Usunięcie po kluczu (×1000)

`S18` · DELETE · łatwy

**Co robi:** Usuwa 1000 transakcji wstawionych w S10, po jednej.

**Query:**

```sql
DELETE FROM transactions WHERE transaction_id = :new_id;   -- 1000 razy
```

- **MongoDB:** `delete_one` w pętli.
- **Redis:** `DEL` + `SREM` / `ZREM` ze wszystkich indeksów, w pętli.

## 19. Usunięcie po filtrze (region + miesiąc)

`S19` · DELETE · średni

**Co robi:** Usuwa transakcje z jednego regionu z jednego miesiąca (inny miesiąc niż w S14), jednym poleceniem.

**Query:**

```sql
DELETE FROM transactions
WHERE region = :region AND transaction_date >= :delete_start AND transaction_date < :delete_end;
```

- **MongoDB:** `delete_many` z filtrem.
- **Redis:** `ZRANGEBYSCORE idx:tx:date` → filtr po regionie → `DEL` + `SREM` / `ZREM` z indeksów.

## 20. Usunięcie ok. 50% najstarszych transakcji

`S20` · DELETE · trudny

**Co robi:** Usuwa wszystkie transakcje starsze niż mediana dat.

**Query:**

```sql
DELETE FROM transactions WHERE transaction_date < :cutoff_date;
```

- **MongoDB:** `delete_many({transaction_date: {$lt: cutoff}})`.
- **Redis:** `ZRANGEBYSCORE idx:tx:date -inf (cutoff` → `DEL` + `SREM` / `ZREM` z indeksów, paczkami.

## 21. Liczba transakcji po usunięciach

`S21` · READ · łatwy

**Co robi:** Zlicza pozostałe transakcje.

**Query:**

```sql
SELECT COUNT(*) FROM transactions;
```

- **MongoDB:** `count_documents({})`.
- **Redis:** `ZCARD idx:tx:date`.

## 22. Przychód per kategoria po usunięciach

`S22` · READ · średni

**Co robi:** To samo co S16, na danych po S18–S20.

**Query:**

```sql
SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.category
ORDER BY revenue DESC, p.category;
```

- **MongoDB:** Jak S16.
- **Redis:** Jak S16.

## 23. Segment × kategoria × region po usunięciach

`S23` · READ · trudny

**Co robi:** To samo co S17, na danych po S18–S20.

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

- **MongoDB:** Jak S17.
- **Redis:** Jak S17.

## 24. Usunięcie wszystkich danych

`S24` · DELETE · trudny

**Co robi:** Usuwa wszystkie dane ze wszystkich trzech tabel. Wynik: liczba pozostałych wierszy (musi być 0).

**Query:**

```sql
DELETE FROM transactions;
DELETE FROM products;
DELETE FROM customers;
```

- **MongoDB:** `delete_many({})` na trzech kolekcjach.
- **Redis:** `FLUSHDB` (czas nieporównywalny z `DELETE` w SQL).
