"""The 32 benchmark scenarios (identical for every database) and the parameters they share.

    uv run python -m zbd.scenarios               # list the scenarios and the parameters for --size
    uv run python -m zbd.scenarios --markdown    # regenerate SCENARIOS.md from this file

Contract for every <db>/Query.py (steps S02..S32; S01 is DataInjection.inject):
    connect()              -> ctx            (opened outside the timed part)
    disconnect(ctx)
    SCENARIOS = {"S02": fn, ..., "S32": fn}   with fn(ctx, params) -> list of tuples (read) or int (affected rows)
"""
import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from textwrap import dedent

from zbd.dataset import ROOT, RetailDataset

PK_SAMPLE = 1000        # point reads / updates / deletes by key
CUSTOMER_SAMPLE = 200   # customers in the "purchases of a customer" scenario
SINGLE_INSERTS = 1000   # one INSERT per row
BATCH_INSERTS = 10_000  # one batch
BULK_INSERTS = 100_000  # the big bulk insert (S12)
NEW_CUSTOMERS = 5_000   # customers created together with their transactions (S13)
TRANSACTIONS_PER_NEW_CUSTOMER = 5
UPDATE_EVERY = 100      # every 100th transaction is updated in S18
HEAVY_SINCE = "2025-01-01"
VIP_SEGMENT = "Top spender"  # segment given to the biggest spenders in S21 (and deleted in S27)
VIP_QUANTILE = 0.8           # customers above this quantile of total spending become VIP
SUMMARY_TABLE = "monthly_sales"  # derived table created in S15


@dataclass(frozen=True)
class Scenario:
    id: str
    operation: str    # CREATE | READ | UPDATE | DELETE
    difficulty: str   # easy | medium | heavy
    name: str
    description: str
    sql: str          # canonical SQL (Postgres flavour); :name = parameter from Params
    mongo: str
    redis: str


_MEDIUM_SQL = """
SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue, SUM(t.quantity) AS quantity
FROM transactions t
JOIN products p USING (product_id)
GROUP BY p.category
ORDER BY revenue DESC, p.category;
"""

_HEAVY_SQL = """
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
"""

SCENARIOS = [
    Scenario(
        "S01", "CREATE", "heavy", "Masowe ładowanie danych",
        "Ładuje `customers`, `products` i N transakcji jednym bulk loadem (razem z indeksami). "
        "Mierzony jest cały `inject()`.",
        """
        COPY customers    (customer_id, ...) FROM STDIN;
        COPY products     (product_id, ...)  FROM STDIN;
        COPY transactions (transaction_id, ...) FROM STDIN;
        CREATE INDEX ON transactions (customer_id);
        CREATE INDEX ON transactions (transaction_date);
        -- MySQL: wielowierszowy INSERT po 10 000 wierszy; indeks po dacie po załadowaniu, potem ANALYZE TABLE;
        -- indeksy kluczy obcych (customer_id, product_id) InnoDB tworzy sam od początku
        """,
        "`insert_many(ordered=False)` paczkami po 50 000 + 2 indeksy na `transactions` (`customer_id`, `transaction_date`). Każda transakcja ma wbudowane pola klienta (segment, grupa wiekowa) i produktu (kategoria, nazwa), więc dokumenty są większe.",
        "Pipeline: `HSET` + `SADD` / `ZADD` (3 indeksy na transakcję: klient, kanał, data).",
    ),
    Scenario(
        "S02", "READ", "easy", "Odczyt po kluczu głównym (×1000)",
        "1000 osobnych zapytań o transakcję po `transaction_id` (id równomiernie rozłożone po całym zbiorze). "
        "Wynik: liczba znalezionych i suma `sales_amount`.",
        """
        SELECT * FROM transactions WHERE transaction_id = :id;   -- 1000 razy
        """,
        "`find_one({_id: id})` w pętli.",
        "`HGETALL transaction:{id}` w pętli.",
    ),
    Scenario(
        "S03", "READ", "easy", "Przychód per region (kanał Online)",
        "Jedna tabela: liczba transakcji i przychód per region dla kanału `Online`.",
        """
        SELECT region, COUNT(*), SUM(sales_amount)
        FROM transactions
        WHERE sales_channel = 'Online'
        GROUP BY region
        ORDER BY region;
        """,
        "`$match` + `$group` + `$sort`.",
        "Identyfikatory z `idx:tx:channel:Online` (`SSCAN`, paczkami) → skrypt Lua w serwerze: `HMGET` + `GROUP BY region`; klient scala paczki.",
    ),
    Scenario(
        "S04", "READ", "easy", "Liczba transakcji z jednego miesiąca",
        "Zliczenie transakcji z zakresu dat jednego miesiąca (używa indeksu po dacie).",
        """
        SELECT COUNT(*) FROM transactions
        WHERE transaction_date >= :count_start AND transaction_date < :count_end;
        """,
        "`count_documents` z filtrem po `transaction_date`.",
        "`ZCOUNT idx:tx:date min max`.",
    ),
    Scenario(
        "S05", "READ", "medium", "Przychód per kategoria (JOIN 2 tabel)",
        "Transakcje połączone z produktami: liczba, przychód per kategoria.",
        """
        SELECT p.category, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN products p USING (product_id)
        GROUP BY p.category
        ORDER BY revenue DESC, p.category;
        """,
        "`$group` po wbudowanej kategorii produktu (bez `$lookup`).",
        "Identyfikatory wszystkich transakcji (`ZRANGE idx:tx:date`, paczkami) → skrypt Lua w serwerze: `HMGET`, kategoria z `HGET product:{id}`, `GROUP BY`; klient scala paczki.",
    ),
    Scenario(
        "S06", "READ", "medium", "Top 10 produktów wg przychodu",
        "Przychód per produkt z nazwą produktu, 10 najlepszych.",
        """
        SELECT p.product_id, p.product_name, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN products p USING (product_id)
        GROUP BY p.product_id, p.product_name
        ORDER BY revenue DESC, p.product_id
        LIMIT 10;
        """,
        "`$group` po `product_id` (nazwa produktu wbudowana, `$first`) → `$sort` → `$limit`.",
        "Jak S05, ale skrypt grupuje po `product_id`; nazwy 10 najlepszych klient bierze ze słownika produktów.",
    ),
    Scenario(
        "S07", "READ", "medium", "Zakupy 200 klientów z nazwami produktów",
        "Dla 200 klientów (równomiernie rozłożonych) pobiera ich transakcje z nazwami produktów (JOIN). "
        "Wynik: liczba wierszy i liczba różnych nazw produktów.",
        """
        SELECT t.transaction_id, p.product_name
        FROM transactions t
        JOIN products p USING (product_id)
        WHERE t.customer_id = :customer_id;   -- 200 razy
        """,
        "`find` po `customer_id` (nazwa produktu wbudowana), w pętli.",
        "`SMEMBERS idx:tx:customer:{id}` → `HMGET product_id`, nazwa ze słownika produktów.",
    ),
    Scenario(
        "S08", "READ", "heavy", "Segment × kategoria × region (JOIN 3 tabel)",
        "Zapytanie `heavy`: przychód per segment klienta, kategoria i region od 2025-01-01, "
        "grupy powyżej 1000, top 20.",
        """
        SELECT c.customer_segment, p.category, t.region, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN customers c USING (customer_id)
        JOIN products p USING (product_id)
        WHERE t.transaction_date >= :heavy_since
        GROUP BY c.customer_segment, p.category, t.region
        HAVING SUM(t.sales_amount) > 1000
        ORDER BY revenue DESC, c.customer_segment, p.category, t.region
        LIMIT 20;
        """,
        "`$match` po dacie + `$group` po wbudowanych polach (segment, kategoria) + `$match` + `$sort` + `$limit`, bez `$lookup`; wymuszony skan kolekcji (`hint $natural`), bo przy 10M planner z indeksem po dacie czytał dokumenty losowo i był wielokrotnie wolniejszy.",
        "Identyfikatory od `heavy_since` (`idx:tx:date`, paczkami) → skrypt Lua: `HMGET`, segment klienta (`HGET customer:{id}`) i kategoria (`HGET product:{id}`), `GROUP BY`; klient scala paczki i filtruje `HAVING`.",
    ),
    Scenario(
        "S09", "READ", "heavy", "Miesięczny przychód per kategoria i grupa wiekowa",
        "JOIN 3 tabel, grupowanie po roku, miesiącu, kategorii i grupie wiekowej klienta; top 20 wg przychodu.",
        """
        SELECT EXTRACT(YEAR FROM t.transaction_date) AS y, EXTRACT(MONTH FROM t.transaction_date) AS m,
               p.category, c.customer_age_group, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN customers c USING (customer_id)
        JOIN products p USING (product_id)
        GROUP BY y, m, p.category, c.customer_age_group
        ORDER BY revenue DESC, y, m, p.category, c.customer_age_group
        LIMIT 20;
        """,
        "`$group` po `$year` / `$month` i wbudowanych polach (kategoria, grupa wiekowa) + `$sort` + `$limit`, bez `$lookup`.",
        "Identyfikatory wszystkich transakcji (paczkami) → skrypt Lua: rok i miesiąc z daty, kategoria i grupa wiekowa przez `HGET`, `GROUP BY`; klient scala paczki.",
    ),
    Scenario(
        "S10", "CREATE", "easy", "Pojedyncze INSERT-y (×1000)",
        "1000 nowych transakcji wstawianych pojedynczo (osobne polecenie na wiersz, jeden commit na końcu). "
        "Nowe transakcje wskazują na istniejących klientów i produkty.",
        """
        INSERT INTO transactions (transaction_id, ..., region) VALUES (...);   -- 1000 razy
        """,
        "`insert_one` w pętli (wcześniej odczyt pól klienta i produktu, które trzeba wbudować).",
        "`HSET` + `SADD` / `ZADD` w pętli (wszystkie indeksy).",
    ),
    Scenario(
        "S11", "CREATE", "medium", "Wstawienie paczki 10 000 transakcji",
        "10 000 kolejnych nowych transakcji wstawionych paczkami.",
        """
        INSERT INTO transactions (transaction_id, ..., region) VALUES (...), (...), ...;   -- paczki, razem 10 000
        """,
        "`insert_many` paczkami (wcześniej odczyt pól klienta i produktu do wbudowania).",
        "Pipeline `HSET` + `SADD` / `ZADD`.",
    ),
    Scenario(
        "S12", "CREATE", "heavy", "Masowe wstawienie 100 000 transakcji",
        "100 000 kolejnych nowych transakcji (istniejący klienci i produkty) wstawionych paczkami, jeden commit na końcu.",
        """
        INSERT INTO transactions (transaction_id, ..., region) VALUES (...), (...), ...;   -- paczki, razem 100 000
        """,
        "`insert_many` paczkami po 5000 (wcześniej odczyt pól klienta i produktu do wbudowania).",
        "Pipeline `HSET` + `SADD` / `ZADD` paczkami po 10 000.",
    ),
    Scenario(
        "S13", "CREATE", "heavy", "Nowi klienci wraz z transakcjami (jedna transakcja)",
        "5000 nowych klientów i po 5 transakcji każdego z nich (25 000), zapisane razem: klienci muszą powstać przed transakcjami, "
        "które na nich wskazują (klucz obcy). Wynik: liczba wstawionych wierszy (klienci + transakcje).",
        """
        INSERT INTO customers (customer_id, ...) VALUES (...), ...;      -- 5000 klientów
        INSERT INTO transactions (transaction_id, ...) VALUES (...), ...; -- 25 000 transakcji
        COMMIT;   -- jedna transakcja na całość
        """,
        "`insert_many` klientów, potem `insert_many` transakcji z wbudowanymi polami klienta i produktu (bez wspólnej transakcji).",
        "Pipeline: `HSET customer:{id}` + `SADD ids:customers`, potem `HSET` + `SADD` / `ZADD` transakcji (bez wspólnej transakcji).",
    ),
    Scenario(
        "S14", "READ", "easy", "Odczyt wstawionych rekordów po kluczu (×1000)",
        "Odczyt po kluczu 1000 transakcji wstawionych w S10 (sprawdza, że insert jest widoczny). "
        "Wynik: liczba znalezionych i suma `sales_amount`.",
        """
        SELECT * FROM transactions WHERE transaction_id = :new_id;   -- 1000 razy
        """,
        "`find_one({_id: id})` w pętli.",
        "`HGETALL transaction:{id}` w pętli.",
    ),
    Scenario(
        "S15", "CREATE", "heavy", "Tabela zagregowana (CREATE TABLE AS, JOIN 3 tabel)",
        "Tworzy nową tabelę `monthly_sales` z wynikiem zapytania: rok × miesiąc × kategoria × grupa wiekowa "
        "(liczba transakcji i przychód), bez `LIMIT`. Wynik: liczba wierszy nowej tabeli. "
        "Tabela (kolekcja, klucze) jest usuwana w S32.",
        """
        CREATE TABLE monthly_sales AS
        SELECT EXTRACT(YEAR FROM t.transaction_date) AS y, EXTRACT(MONTH FROM t.transaction_date) AS m,
               p.category, c.customer_age_group, COUNT(*) AS n, SUM(t.sales_amount) AS revenue
        FROM transactions t
        JOIN customers c USING (customer_id)
        JOIN products p USING (product_id)
        GROUP BY 1, 2, 3, 4;
        """,
        "`$group` po `$year` / `$month` i wbudowanych polach + `$out` do kolekcji `monthly_sales`.",
        "Jak S09 bez limitu (skrypt Lua w serwerze + scalanie w kliencie), potem `HSET monthly_sales:{...}` dla każdej grupy.",
    ),
    Scenario(
        "S16", "UPDATE", "easy", "Update po kluczu (×1000)",
        "`quantity + 1` dla 1000 transakcji, osobne polecenie na każdą (jeden commit na końcu).",
        """
        UPDATE transactions SET quantity = quantity + 1 WHERE transaction_id = :id;   -- 1000 razy
        """,
        "`update_one` z `$inc` w pętli.",
        "`HINCRBY transaction:{id} quantity 1` tylko dla istniejących kluczy (skrypt Lua).",
    ),
    Scenario(
        "S17", "UPDATE", "medium", "Update po filtrze (region + miesiąc)",
        "`discount_pct + 1` dla wszystkich transakcji z jednego regionu w jednym miesiącu (jedno polecenie).",
        """
        UPDATE transactions SET discount_pct = discount_pct + 1
        WHERE region = :region AND transaction_date >= :update_start AND transaction_date < :update_end;
        """,
        "`update_many` z `$inc`.",
        "`ZRANGEBYSCORE idx:tx:date` → filtr po regionie → `HINCRBY discount_pct`.",
    ),
    Scenario(
        "S18", "UPDATE", "heavy", "Masowy update co setnej transakcji",
        "`quantity + 1` dla co 100. transakcji (1% zbioru) paczkami po 1000 id.",
        """
        UPDATE transactions SET quantity = quantity + 1
        WHERE transaction_id IN (:id_1, ..., :id_1000);   -- paczki
        """,
        "`update_many({_id: {$in: [...]}}, {$inc: {quantity: 1}})` paczkami.",
        "Pipeline `HINCRBY` (skrypt Lua) paczkami po 10 000.",
    ),
    Scenario(
        "S19", "UPDATE", "heavy", "Update całej tabeli z warunkiem (2 pola, pełny skan)",
        "`quantity + 1` i `discount_pct + 1` dla wszystkich transakcji z `discount_pct` nie mniejszym niż mediana "
        "(pole bez indeksu, więc każda baza czyta cały zbiór), jednym poleceniem.",
        """
        UPDATE transactions SET quantity = quantity + 1, discount_pct = discount_pct + 1
        WHERE discount_pct >= :discount_min;
        """,
        "`update_many` z `$inc` na dwóch polach.",
        "Identyfikatory wszystkich transakcji (paczkami po 50 000) → skrypt Lua: `HGET discount_pct`, `HINCRBY` dwóch pól dla pasujących.",
    ),
    Scenario(
        "S20", "UPDATE", "heavy", "Update przez JOIN (wszystkie transakcje jednej kategorii)",
        "`discount_pct + 2` dla wszystkich transakcji produktów z jednej kategorii: kategoria jest w tabeli `products`, "
        "więc update łączy dwie tabele.",
        """
        UPDATE transactions t SET discount_pct = t.discount_pct + 2
        FROM products p
        WHERE p.product_id = t.product_id AND p.category = :update_category;
        -- MySQL: UPDATE transactions t JOIN products p ON ... SET ... WHERE p.category = ...
        """,
        "`update_many({\"product.category\": ...}, {$inc: ...})` na wbudowanym polu (bez joina).",
        "Identyfikatory produktów danej kategorii → skrypt Lua po wszystkich transakcjach (paczkami po 50 000): `HGET product_id`, `HINCRBY` dla pasujących.",
    ),
    Scenario(
        "S21", "UPDATE", "heavy", "Awans największych klientów (update z agregacją)",
        "Klienci, których łączny przychód jest powyżej 80. percentyla, dostają segment `Top spender`. "
        "Agregacja po całej tabeli `transactions` steruje update'em `customers`; skutek widać w S23, S27 i S31. "
        "Wynik: liczba zmienionych klientów.",
        """
        UPDATE customers SET customer_segment = 'Top spender'
        WHERE customer_id IN (
            SELECT customer_id FROM transactions
            GROUP BY customer_id HAVING SUM(sales_amount) > :vip_threshold
        );
        """,
        "`$group` po `customer_id` + `$match` → paczkami po 1000 id: `update_many` w `customers` oraz `update_many` wbudowanego `customer.segment` w `transactions` (po indeksie `customer_id`).",
        "Identyfikatory klientów (`SSCAN ids:customers`, paczkami) → skrypt Lua: suma `sales_amount` z `idx:tx:customer:{id}`, `HSET customer_segment` dla przekraczających próg.",
    ),
    Scenario(
        "S22", "READ", "medium", "Przychód per kategoria po update'ach",
        "To samo co S05, ale dodatkowo `SUM(quantity)`: widać skutek update'ów S16–S21.",
        _MEDIUM_SQL,
        "Jak S05 + `$sum: \"$quantity\"`.",
        "Jak S05 + suma `quantity`.",
    ),
    Scenario(
        "S23", "READ", "heavy", "Segment × kategoria × region po update'ach",
        "To samo co S08, ale dodatkowo `SUM(quantity)`.",
        _HEAVY_SQL,
        "Jak S08 + `$sum: \"$quantity\"`.",
        "Jak S08 + suma `quantity`.",
    ),
    Scenario(
        "S24", "DELETE", "easy", "Usunięcie po kluczu (×1000)",
        "Usuwa 1000 transakcji wstawionych w S10, po jednej.",
        """
        DELETE FROM transactions WHERE transaction_id = :new_id;   -- 1000 razy
        """,
        "`delete_one` w pętli.",
        "`DEL` + `SREM` / `ZREM` ze wszystkich indeksów, w pętli.",
    ),
    Scenario(
        "S25", "DELETE", "medium", "Usunięcie po filtrze (region + miesiąc)",
        "Usuwa transakcje z jednego regionu z jednego miesiąca (inny miesiąc niż w S17), jednym poleceniem.",
        """
        DELETE FROM transactions
        WHERE region = :region AND transaction_date >= :delete_start AND transaction_date < :delete_end;
        """,
        "`delete_many` z filtrem.",
        "`ZRANGEBYSCORE idx:tx:date` → filtr po regionie → `DEL` + `SREM` / `ZREM` z indeksów.",
    ),
    Scenario(
        "S26", "DELETE", "heavy", "Usunięcie ok. 50% najstarszych transakcji",
        "Usuwa wszystkie transakcje starsze niż mediana dat.",
        """
        DELETE FROM transactions WHERE transaction_date < :cutoff_date;
        """,
        "`delete_many({transaction_date: {$lt: cutoff}})` z `hint $natural` (skan kolekcji zamiast indeksu po dacie); gdyby serwer odrzucił hint przy usuwaniu, wykonuje się zwykłe `delete_many`.",
        "`ZRANGEBYSCORE idx:tx:date -inf (cutoff` → `DEL` + `SREM` / `ZREM` z indeksów, paczkami.",
    ),
    Scenario(
        "S27", "DELETE", "heavy", "Usunięcie transakcji klientów `Top spender` (podzapytanie)",
        "Usuwa wszystkie transakcje klientów, którzy w S21 dostali segment `Top spender` (segment jest w tabeli `customers`).",
        """
        DELETE FROM transactions
        WHERE customer_id IN (SELECT customer_id FROM customers WHERE customer_segment = 'Top spender');
        """,
        "`delete_many({\"customer.segment\": \"Top spender\"})` na wbudowanym polu (skan kolekcji).",
        "`HGET customer_segment` po `ids:customers` → dla każdego takiego klienta `SMEMBERS idx:tx:customer:{id}` → `DEL` + `SREM` / `ZREM` z indeksów, paczkami.",
    ),
    Scenario(
        "S28", "DELETE", "heavy", "Usunięcie klientów bez transakcji (anty-join)",
        "Usuwa z `customers` klientów, którzy po S24–S27 nie mają już żadnej transakcji (m.in. wszystkich `Top spender`). "
        "Wynik: liczba usuniętych klientów.",
        """
        DELETE FROM customers c
        WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.customer_id = c.customer_id);
        """,
        "`$lookup` z `$limit: 1` (po indeksie `customer_id`) + `$match` pustych wyników → `delete_many({_id: {$in: [...]}})` paczkami.",
        "`SCARD idx:tx:customer:{id}` dla każdego z `ids:customers` → `DEL customer:{id}` + `SREM ids:customers` dla pustych.",
    ),
    Scenario(
        "S29", "READ", "easy", "Liczba transakcji po usunięciach",
        "Zlicza pozostałe transakcje.",
        """
        SELECT COUNT(*) FROM transactions;
        """,
        "`count_documents({})`.",
        "`ZCARD idx:tx:date`.",
    ),
    Scenario(
        "S30", "READ", "medium", "Przychód per kategoria po usunięciach",
        "To samo co S22, na danych po S24–S28.",
        _MEDIUM_SQL,
        "Jak S22.",
        "Jak S22.",
    ),
    Scenario(
        "S31", "READ", "heavy", "Segment × kategoria × region po usunięciach",
        "To samo co S23, na danych po S24–S28.",
        _HEAVY_SQL,
        "Jak S23.",
        "Jak S23.",
    ),
    Scenario(
        "S32", "DELETE", "heavy", "Usunięcie wszystkich danych",
        "Usuwa wszystkie dane ze wszystkich trzech tabel oraz tabelę `monthly_sales` z S15. "
        "Wynik: liczba pozostałych wierszy (musi być 0).",
        """
        DELETE FROM transactions;
        DELETE FROM products;
        DELETE FROM customers;
        DROP TABLE monthly_sales;
        """,
        "`delete_many({})` na trzech kolekcjach + `drop` kolekcji `monthly_sales`.",
        "`FLUSHDB` (czas nieporównywalny z `DELETE` w SQL).",
    ),
]

SCENARIO_IDS = [s.id for s in SCENARIOS]


def month_range(year_month):
    """('2024-06-01', '2024-07-01') for '2024-06' (end is exclusive)."""
    year, month = map(int, year_month.split("-"))
    end = date(year + month // 12, month % 12 + 1, 1)
    return f"{year_month}-01", end.isoformat()


class Params:
    """Inputs shared by every database, derived deterministically from the dataset,
    so every database gets exactly the same ids, filters and new rows."""

    def __init__(self, dataset):
        base = dataset.base_rows()
        self.size = dataset.size
        self.heavy_since = HEAVY_SINCE

        n = min(PK_SAMPLE, dataset.size)
        self.pk_ids = [dataset.transaction_id_at(i * dataset.size // n) for i in range(n)]
        self.customer_ids = dataset.sample_customer_ids(CUSTOMER_SAMPLE)
        self.update_ids = dataset.transaction_ids(UPDATE_EVERY)

        rows = []
        for i in range(SINGLE_INSERTS + BATCH_INSERTS):
            source = base[(i * 7919) % len(base)]  # 7919 is prime: spreads the new rows over the source
            rows.append({
                **source,
                "transaction_id": f"X{i:08d}",
                "customer_id": dataset.copy0_customer_id(source["customer_id"]),
            })
        self.new_single = rows[:SINGLE_INSERTS]
        self.new_batch = rows[SINGLE_INSERTS:]
        self.new_single_ids = [row["transaction_id"] for row in self.new_single]

        # S12: a big bulk insert of transactions of existing customers
        self.new_bulk = []
        for i in range(BULK_INSERTS):
            source = base[(i * 7919) % len(base)]
            self.new_bulk.append({**source, "transaction_id": f"Y{i:08d}",
                                  "customer_id": dataset.copy0_customer_id(source["customer_id"])})

        # S13: brand new customers (attributes copied from existing ones) with their first transactions
        self.new_customers = [
            {**dataset.customers[(i * 31) % len(dataset.customers)], "customer_id": f"Z{i:06d}"}
            for i in range(NEW_CUSTOMERS)
        ]
        self.new_customer_transactions = []
        for i, customer in enumerate(self.new_customers):
            for j in range(TRANSACTIONS_PER_NEW_CUSTOMER):
                k = i * TRANSACTIONS_PER_NEW_CUSTOMER + j
                self.new_customer_transactions.append({
                    **base[(k * 104729) % len(base)],  # 104729 is prime
                    "transaction_id": f"N{k:08d}",
                    "customer_id": customer["customer_id"],
                })

        # S19: full-table update, S20: update through a join, S21 / S27: customers by total spending
        discounts = sorted(int(row["discount_pct"]) for row in base)
        self.discount_min = discounts[len(discounts) // 2]
        self.update_category = sorted({product["category"] for product in dataset.products})[0]
        self.vip_segment = VIP_SEGMENT
        spent = {}
        for row in base:
            spent[row["customer_id"]] = spent.get(row["customer_id"], 0) + round(Decimal(row["sales_amount"]) * 100)
        totals = sorted(spent.values())
        # half a cent above a real total: no sum (cents) can tie with it, so Decimal and float sums agree
        self.vip_threshold = Decimal(totals[int(len(totals) * VIP_QUANTILE)]) / 100 + Decimal("0.005")

        months = sorted({row["transaction_date"][:7] for row in base})
        self.count_start, self.count_end = month_range(months[len(months) // 4])
        self.update_start, self.update_end = month_range(months[len(months) // 2])
        self.delete_start, self.delete_end = month_range(months[min(len(months) // 2 + 1, len(months) - 1)])
        self.region = sorted({row["region"] for row in base})[0]
        dates = sorted(row["transaction_date"] for row in base)
        self.cutoff_date = dates[len(dates) // 2]

    def summary(self):
        return {
            "size": self.size,
            "pk_ids": f"{len(self.pk_ids)} ({self.pk_ids[0]} ... {self.pk_ids[-1]})",
            "customer_ids": f"{len(self.customer_ids)} ({self.customer_ids[0]} ...)",
            "update_ids (every 100th)": len(self.update_ids),
            "new_single / new_batch / new_bulk": f"{len(self.new_single)} / {len(self.new_batch)} / {len(self.new_bulk)}",
            "new customers / their transactions (S13)": f"{len(self.new_customers)} / {len(self.new_customer_transactions)}",
            "discount_min (S19)": self.discount_min,
            "update_category (S20)": self.update_category,
            "vip_threshold (S21)": self.vip_threshold,
            "region": self.region,
            "count month (S04)": (self.count_start, self.count_end),
            "update month (S17)": (self.update_start, self.update_end),
            "delete month (S25)": (self.delete_start, self.delete_end),
            "cutoff_date (S26)": self.cutoff_date,
            "heavy_since": self.heavy_since,
        }


DIFFICULTY_PL = {"easy": "łatwy", "medium": "średni", "heavy": "trudny"}

MARKDOWN_INTRO = """\
# Scenariusze testowe

{total} scenariuszy wykonywanych w tej samej kolejności na każdej bazie (PostgreSQL, MySQL, MongoDB, Redis).
Ten plik jest generowany z `zbd/scenarios.py` (`uv run python -m zbd.scenarios --markdown`), nie edytuj go ręcznie.

- **Run** to jeden pełny przebieg S01–S{total} dla jednego rozmiaru danych. Każdy rozmiar (100 000, 1 000 000,
  10 000 000 transakcji) jest powtarzany 3 razy, razem 9 runów na bazę.
- Kolejność jest stała, bo scenariusze zależą od siebie: S01 ładuje dane, S10–S13 dokładają nowe rekordy,
  S15 tworzy tabelę zagregowaną, S16–S21 zmieniają dane (S22–S23 pokazują skutek), S24–S28 usuwają
  (S29–S31 pokazują skutek), S32 sprząta.
- Parametry (id, filtry, nowe rekordy) są liczone deterministycznie z danych (`Params` w `zbd/scenarios.py`),
  więc każda baza dostaje dokładnie to samo. Wyniki scenariuszy są porównywane między bazami.
- Podział: {difficulty}. Typy: {operations}.
- Zapytania poniżej są w kanonicznym SQL (Postgres); `:nazwa` to parametr z `Params`.
  Warianty MongoDB i Redis opisują, jak to samo jest zrobione w tych bazach.

| # | Nazwa | Operacja | Trudność |
|---|---|---|---|
"""


def markdown_intro():
    difficulty = ", ".join(f"{sum(s.difficulty == d for s in SCENARIOS)} {name}"
                           for d, name in [("easy", "łatwych"), ("medium", "średnich"), ("heavy", "trudnych")])
    operations = ", ".join(f"{sum(s.operation == op for s in SCENARIOS)} × {op}"
                           for op in ["CREATE", "READ", "UPDATE", "DELETE"])
    return MARKDOWN_INTRO.format(total=len(SCENARIOS), difficulty=difficulty, operations=operations)


def markdown():
    lines = [markdown_intro().rstrip("\n")]
    for number, s in enumerate(SCENARIOS, 1):
        lines.append(f"| {number} | {s.name} | {s.operation} | {DIFFICULTY_PL[s.difficulty]} |")
    for number, s in enumerate(SCENARIOS, 1):
        lines += [
            "",
            f"## {number}. {s.name}",
            "",
            f"`{s.id}` · {s.operation} · {DIFFICULTY_PL[s.difficulty]}",
            "",
            f"**Co robi:** {s.description}",
            "",
            "**Query:**",
            "",
            "```sql",
            dedent(s.sql).strip(),
            "```",
            "",
            f"- **MongoDB:** {s.mongo}",
            f"- **Redis:** {s.redis}",
        ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--markdown", action="store_true", help="write SCENARIOS.md")
    parser.add_argument("--size", type=int, default=100_000, help="dataset size for the parameter summary")
    args = parser.parse_args()

    if args.markdown:
        path = ROOT / "SCENARIOS.md"
        path.write_text(markdown())
        print(f"wrote {path}")
        return

    for number, s in enumerate(SCENARIOS, 1):
        print(f"{number:>2} {s.id} {s.operation:<7} {s.difficulty:<7} {s.name}")
    print(f"\nparameters for {args.size:,} transactions:")
    for key, value in Params(RetailDataset(size=args.size).load()).summary().items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
