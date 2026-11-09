# TASKS — Bağımlılık Planı

Durum: ☐ bekliyor · ◐ sürüyor · ☑ bitti. Kapı = pytest+ruff yeşil.

## Dalga 0 — Temel (Master) ☑
- ☑ Repo iskeleti, requirements, pyproject, .env.example, .gitignore
- ☑ Pydantic sözleşmeleri (`backend/schemas`)
- ☑ Demo fabrika (`backend/demo`), modül stub'ları
- ☑ ARCHITECTURE.md, TASKS.md, `.claude/agents/*`

## Dalga 1 — paralel (3 ajan) ◐  [bağımsız; yalnız sözleşmelere bağlı]
| ID | Ajan | İş | Kabul |
|---|---|---|---|
| W1-BE | backend-engineer (Sonnet) | SQLAlchemy tabloları (15 tablo), config/settings, services.py (proje/layout sürüm/sim/opt kayıt, demo yükleme, JSON/CSV/Excel import-export), FastAPI uçları, Google Sheets/Drive adaptör arayüzü (OAuth yokken devre dışı) | DB round-trip testleri, API TestClient testleri |
| W1-LR | layout-routing-engineer (Sonnet) | validate_layout, editing, koridor grafı, A*/Dijkstra, tek yön/araç kısıtı/engel, derive_flow_edges, FlowMetrics, Plotly spaghetti/heat/before-after/layout | Çakışma, erişilebilirlik, bilinen mesafe, D_total testleri; demo layout valid |
| W1-SIM | simpy-engineer (Sonnet) | SimPy motoru, olay logu, KPI, replikasyon+CI, CRN, bottleneck, compare_results | Olay sırası, kapasite, kuyruk, WIP, Little yasası, seed tekrarlanabilirliği |

Kapı G1: merge + pytest + ruff.

## Dalga 2 — paralel (3 ajan) ☐  [G1'e bağlı]
| ID | Ajan | İş | Bağımlılık |
|---|---|---|---|
| W2-OPT | optimization-engineer (**Opus**) | swap/local/SA + OR-Tools atama, kısıtlı uygulanabilirlik, ≥3 alternatif, CRN sim, hedef %X azaltma | W1-LR, W1-SIM |
| W2-AI | gemini-engineer (Sonnet) | gemini_client, 18 tool, executor, orchestrator, onay akışı, kota/backoff/cache, FakeGeminiClient, `analyze_facility` pipeline bağlama | W1-BE (+LR/SIM) |
| W2-UI | frontend-reporting-engineer (Sonnet) | Rapor motoru (HTML/PDF/XLSX/PNG, 14 bölüm), Streamlit (10 sayfa, sağ panel AI), TR arayüz | W1 hepsi |

Kapı G2: merge + pytest + ruff.

## Dalga 3 — Entegrasyon & QA ☐
- Master: `services.analyze_facility` uçtan uca bağlama, README, docs/, cilalama
- W3-QA qa-engineer (Sonnet): bağımsız doğrulama; test_end_to_end; rapor↔hesap tutarlılık; offline mod; hata raporu `QA_REPORT.md`
- Master: QA bulgularını ilgili ajanlara geri yönlendir, düzelt, kapat

## Milestone eşleşmesi (master prompt §18)
M1 Foundation = W1-BE+W2-AI iskeleti · M2 Layout+AI = W1-LR+W2-AI · M3 Spaghetti = W1-LR · M4 SimPy = W1-SIM · M5 Opt+Report = W2-OPT+W2-UI
