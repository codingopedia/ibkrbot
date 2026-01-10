# STATUS - InteractiveBrockers_BOT (offline snapshot)

## Srodowisko
- Python: 3.10.11 (`.venv`)
- pip: 25.3 (ib-insync 0.9.86, typer 0.21.1, pydantic 2.12.5, tzdata 2025.3); pelna lista: `run/pip_freeze.txt`
- Log komend: `run/status_report.log`
- Pytest: 19 passed / 0 warnings

## Architektura (10-20 linii)
- Typer CLI w `trader/main.py` (`run`, `smoke`, `doctor`, `flatten`, `report`); `_make_broker` wybiera `SimBroker` vs `IBKRBroker`
- YAML -> `AppConfig`/`InstrumentConfig`/`RiskConfig` w `trader/config.py`; JSON logging w `trader/logging_setup.py`
- Market data -> `MarketEvent` (`trader/events.py`) -> strategia (`Strategy`/`NoopStrategy`) -> `OrderIntent`
- Risk guard (`trader/risk/limits.py`) sprawdza max order size / position; `halted` zatrzymuje petle
- Ledger (`trader/ledger/ledger.py`) trzyma pozycje, realized/unrealized PnL, prowizje; uzywa multiplieru instrumentu
- Persistence (`trader/persistence/db.py` + `schema.sql`): sqlite z tabelami orders/fills/pnl_snapshots; exec_id z unikalnym indeksem
- Broker adapters: `trader/broker/sim.py` (synchron generuje ticki, natychmiastowe fill MKT), `trader/broker/ibkr/adapter.py` (ib_insync; kontrakt futures auto/expiry, tick sub, place/cancel, fills/commissions/status)
- Reconcile (`trader/reconcile.py`): przy starcie IBKR porownuje open orders/positions vs DB/ledger, moze ustawic `risk.halt`
- Main loop (`run`): subskrypcja rynku, polling fills/status/commissions, insert PNLSnapshot, risk check, opcjonalne iterations
- Smoke test: tick + 1 order (MKT/LMT) + opcjonalne czekanie na fill + PnL snapshot
- Doctor: read-only IBKR diag (serverVersion, managedAccounts, nearest contracts, pierwszy tick)
- Flatten: kasuje open orders (DB + broker), pobiera pozycje, wysyla MKT po przeciwnej stronie, opcjonalny symbol override
- Reporting: `daily_report.load_pnl_series` czyta PnL z sqlite na potrzeby mini-raportu
- EventBus (`trader/bus.py`) dostepny, ale obecnie petla glowna uzywa bezposrednich callbackow

## Kluczowe pliki / rola
- `trader/main.py`: CLI, petla run/smoke/doctor/flatten, integracja risk/ledger/db/broker
- `trader/config.py`: schemat pydantic dla env/log/storage/runtime/broker/instrument/risk
- `trader/broker/sim.py`: symulator tickow/filli, natychmiastowe wypelnienia MKT, flatten lokalny
- `trader/broker/ibkr/adapter.py`: adapter IBKR (contract resolve, market data, placeOrder, polling fills/status/commission, cancel/flatten)
- `trader/persistence/db.py` + `schema.sql`: tworzenie/migracja sqlite (orders, fills z exec_id, pnl_snapshots)
- `trader/ledger/ledger.py`: w pamieci pozycje, realized/unrealized PnL + commissions
- `trader/risk/limits.py`: limity max_position/max_order_size/max_daily_loss, halt flag
- `trader/reconcile.py`: sprawdza IBKR open trades/positions vs DB, oznacza MissingOnBroker lub haltuje
- `trader/reporting/daily_report.py`: odczyt serii PnL z sqlite
- `tests/*.py`: pokrycie bus, DB migracji/orders, ledger PnL, reconcile edge cases, flatten CLI (sim)

## Baza danych (run/trader.sqlite)
```
tables: ['fills', 'orders', 'pnl_snapshots', 'sqlite_sequence']
columns orders:
  client_order_id TEXT notnull=0 dflt=None pk=1
  broker_order_id TEXT notnull=0 dflt=None pk=0
  symbol TEXT notnull=1 dflt=None pk=0
  side TEXT notnull=1 dflt=None pk=0
  qty INTEGER notnull=1 dflt=None pk=0
  order_type TEXT notnull=1 dflt=None pk=0
  limit_price REAL notnull=0 dflt=None pk=0
  status TEXT notnull=1 dflt=None pk=0
  created_ts TEXT notnull=1 dflt=None pk=0
  updated_ts TEXT notnull=1 dflt=None pk=0
columns fills:
  id INTEGER notnull=0 dflt=None pk=1
  ts TEXT notnull=1 dflt=None pk=0
  client_order_id TEXT notnull=1 dflt=None pk=0
  broker_order_id TEXT notnull=1 dflt=None pk=0
  symbol TEXT notnull=1 dflt=None pk=0
  side TEXT notnull=1 dflt=None pk=0
  qty INTEGER notnull=1 dflt=None pk=0
  price REAL notnull=1 dflt=None pk=0
  commission REAL notnull=1 dflt=None pk=0
  exec_id TEXT notnull=0 dflt=None pk=0
columns pnl_snapshots:
  id INTEGER notnull=0 dflt=None pk=1
  ts TEXT notnull=1 dflt=None pk=0
  symbol TEXT notnull=1 dflt=None pk=0
  position_qty INTEGER notnull=1 dflt=None pk=0
  avg_price REAL notnull=1 dflt=None pk=0
  last_price REAL notnull=0 dflt=None pk=0
  unrealized_usd REAL notnull=1 dflt=None pk=0
  realized_usd REAL notnull=1 dflt=None pk=0
  commissions_usd REAL notnull=1 dflt=None pk=0
```

## Konfiguracja
- Broker: `broker.type` = sim/ibkr; IBKR: `host`, `port` (paper GW 4002 / TWS sample 7497), `client_id`, `market_data_type` (3=delayed), `connect_timeout_seconds`
- Instrument: `symbol`, `exchange`, `currency`, `multiplier`, opcjonalnie `expiry` (YYYYMM/YYMMDD)
- Risk: `max_position`, `max_daily_loss_usd` (haltuje gdy suma realized+unrealized-komisje <= -limit), `max_order_size`
- Orders: brak globalnych presetow TIF/outside_rth w cfg; IBKR adapter uzywa domyslnych MKT/LMT bez TIF override
- Sample configi: `config/paper.example.yaml` (sim, dry run), `config/paper.ibkr.example.yaml` (paper GW), `config/paper.tws.local.yaml` (TWS 7497, heartbeat 1s), `config/live.example.yaml` (sim placeholder)
- `.gitignore` ignoruje `config/*.local.yaml` i artefakty `run/` (sqlite, logi)

## Co dziala (checked)
- ✅ Pytest suite (unit) przechodzi; flatten CLI w trybie sim pokryty testem
- ⚪ polaczenie do TWS: UNVERIFIED OFFLINE (brak live/paper polaczenia w tym raporcie)
- ⚪ doctor: UNVERIFIED OFFLINE (tylko help sprawdzony)
- ⚪ smoke MKT + fill + DB + PnL + commission backfill: UNVERIFIED OFFLINE (wymaga IBKR)
- ⚪ smoke LMT --no-wait: UNVERIFIED OFFLINE (wymaga IBKR)
- ✅ reconcile/halt na open order: pokryte testami jednostkowymi (symulacja IB open trades/positions); runtime IBKR wymaga online
- ✅ flatten --confirm: dziala w trybie sim (test CLI), sciezka IBKR nie sprawdzona offline

## Znane problemy / ryzyka
- ⚠️ Real-time market data moze byc niedostepne (market_data_type=3 -> delayed; tick NaN mozliwy bez uprawnien)
- ⚠️ IBKR TIF preset / error 10349 potencjalny przy braku domyslnego TIF w koncie (brak jawnej konfiguracji)
- ⚠️ Reconcile: obce otwarte zlecenia na koncie lub rozjazd pozycji zatrzymaja bota (wymaga swiadomego podpisu/namespace zlecen)
- ⚠️ Papierowy-only stan: brak potwierdzonego polaczenia/live filli; strategia to Noop
- ⚠️ Sim broker uproszczony (natychmiastowe fill MKT, brak slippage/latency) -> wyniki nieodzwierciedlaja rynku

## Nastepne kroki (priorytety)
- P0: Uruchomic `doctor` na paper (config/paper.tws.local.yaml) - potwierdzic connectivity/ticki/contract lookup; weryfikacja: komenda przechodzi z tickiem
- P0: Smoke MKT (paper) z minimalnym qty - sprawdzic insert order/fill/pnl_snapshot/commission backfill; weryfikacja: logi fill + commission + DB wpisy
- P0: Test reconcile z otwartym LMT (paper) - zostawic LMT no-wait, uruchomic `run` i upewnic sie, ze halt dziala/DB oznacza MissingOnBroker; weryfikacja: log `halted` + status w orders
- P1: Zweryfikowac flatten na IBKR (paper) z faktyczna pozycja - weryfikacja: pozycja = 0, brak open orders po komendzie
- P1: Dodac jawne TIF/outsideRTH do configu/adaptera (eliminuje 10349) - weryfikacja: brak warningu w logach IBKR przy submit
- P1: Obsluzyc brak tickow (timeout/retry + fallback delayed) - weryfikacja: smoke/doctor nie konczy sie wyjatkiem na pustym ticku
- P2: Dodac prosta strategie demo z ograniczonym ryzykiem (np. heartbeat market buy/sell sandbox) - weryfikacja: sygnaly generuja intents, risk przepuszcza, PnL zapisuje sie
- P2: Raport PnL/positions (CLI lub notebook) z `pnl_snapshots` - weryfikacja: komenda/report pokazuje serie z sqlite

## Opcjonalny online check
- `trader doctor -c config/paper.tws.local.yaml`: POMINIETO (offline, brak pewnosci co do TWS); do uruchomienia recznie gdy TWS aktywne
