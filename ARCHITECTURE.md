# FactoryFlow AI — ARCHITECTURE

Master Architect: Lead Orchestrator. Kaynak: `FactoryFlow_AI_Master_Prompt_V2.md`. Birimler: metre, saniye. Orijin sol-alt, +y yukarı.

## 1. Katmanlar

```
Streamlit (frontend/)  ──HTTP──▶ FastAPI (backend/api) ─┐
        │ (in-process import da mümkün)                 ▼
        └──────────────▶ backend/services.py  ◀── Agent tools (backend/agent)
                              │  (TEK giriş noktası)
        ┌──────────┬──────────┼───────────┬──────────────┬───────────┐
     layout/    routing/   spaghetti/  simulation/   optimization/  reporting/
   (validate,  (A*/Dijkstra, (Plotly)   (SimPy)        (SA/swap/     (HTML/PDF/
    edit)       From-To)                 +analytics/    local/OR-T)   XLSX/PNG)
                              │
                       database/ (SQLAlchemy, SQLite)
```

**Kural 1:** Hesaplama YALNIZ domain modüllerinde yapılır. Gemini KPI hesaplamaz; tool çağırır, sonucu yorumlar.
**Kural 2:** Modüller yalnız `backend/schemas` tipleriyle konuşur (saf fonksiyonlar; DB/HTTP bilmezler).
**Kural 3:** UI, API ve Agent domain modüllerini doğrudan değil `backend/services.py` üzerinden çağırır.
**Kural 4:** Aynı rota seti hem Spaghetti'de hem SimPy'de kullanılır (`RouteSet` paylaşılır).

## 2. Veri sözleşmeleri (`backend/schemas/`) — DONDURULDU

| Dosya | Ana tipler |
|---|---|
| common.py | Point, Rect, VehicleType, StationKind |
| layout.py | Facility, Workstation, Corridor, Obstacle, Layout, ValidationIssue/Report |
| flow.py | Product, RouteStep, FlowEdge, Route, RouteSet, FromToCell, FlowMetrics |
| simulation.py | SimConfig, ResourceConfig, CostConfig, EventRecord, SimKPIs, SimResult, CI |
| optimization.py | OptimizationConfig, LayoutAlternative, OptimizationResult, KpiDelta, ScenarioComparison |
| project.py | Project (layout+products+flow+sim_config+cost) |
| agent.py | ToolResult, PendingChange, ToolCallRecord, GeminiStatus, AgentReply |

**Değişiklik prosedürü:** Sözleşme alanı eklemek/değiştirmek yalnız Master Architect'in işidir. Ajan ihtiyaç görürse raporunda "CONTRACT REQUEST" olarak yazar; **kırıcı değişiklik yapmaz**.

## 3. Modül giriş noktaları (imzalar stub'larda sabit)

| Fonksiyon | Dosya | Sahip |
|---|---|---|
| validate_layout | layout/validation.py | Layout&Routing |
| move/add/remove_workstation, clone_layout | layout/editing.py | Layout&Routing |
| derive_flow_edges, compute_routes, compute_flow_metrics | routing/engine.py | Layout&Routing |
| spaghetti_figure, heatmap_figure, before_after_figure, layout_figure | spaghetti/chart.py | Layout&Routing |
| run_simulation | simulation/engine.py | SimPy |
| analyze_bottlenecks, compare_results | analytics/bottleneck.py | SimPy |
| optimize_layout | optimization/engine.py | Optimization |
| build_report_context, export_html/pdf/excel/png | reporting/builder.py | Frontend&Reporting |
| services.* | backend/services.py | Backend |
| tool_registry/executor/orchestrator/gemini_client | backend/agent/ | Gemini |

## 4. Dosya sahipliği (çakışma yasağı)

| Ajan | Model | Sahip olduğu yollar |
|---|---|---|
| backend-engineer | Sonnet | backend/main.py, api/, config/, database/, models/, services.py, integrations/, data/demo_factory/, tests/test_services*.py, tests/test_api.py |
| layout-routing-engineer | Sonnet | backend/layout/, routing/, spaghetti/, tests/test_layout.py, test_routing.py, test_spaghetti.py |
| simpy-engineer | Sonnet | backend/simulation/, analytics/, tests/test_simulation.py, test_analytics.py, docs/simulation_methodology.md |
| optimization-engineer | **Opus** | backend/optimization/, tests/test_optimization.py |
| gemini-engineer | Sonnet | backend/agent/, tests/test_agent_tools.py, tests/fakes/ |
| frontend-reporting-engineer | Sonnet | frontend/, backend/reporting/, tests/test_reports.py |
| qa-engineer | Sonnet | tests/test_end_to_end.py, tests/qa/, QA_REPORT.md (üretim koduna DOKUNMAZ) |
| **master-architect** | Opus | backend/schemas/, backend/demo/, backend/pipeline.py (analyze_facility), ARCHITECTURE.md, TASKS.md, .claude/, requirements, pyproject, README, entegrasyon |

Her ajan ayrı **git worktree**'de çalışır (branch `agent/<ad>`); Master birleştirir. Başka ajanın dosyası gerekiyorsa değiştirmez, rapora yazar.

## 5. Gemini Agent

`user → GeminiClient(function calling) → ToolExecutor(validate args via Pydantic, max N call, task_id log) → services/domain → ToolResult → Gemini açıklaması`.
- 18 tool adı master prompt §5 ile aynı. Layout değiştiren tool'lar (`update_workstation`, `optimize_factory_layout` uygulaması, `import_factory_layout`) `NEEDS_APPROVAL` + `PendingChange` döner; kalıcı yazma yalnız onaydan sonra.
- API: yalnız `.env`; anahtar loglanmaz; durum göstergesi `GeminiStatus`; istek/token sayacı, günlük bütçe, backoff, timeout, yerel önbellek.
- Dış API'ye veri gitmeden önce tek seferlik kullanıcı onayı (`settings.external_data_consent`).
- Testler `FakeGeminiClient` ile; gerçek bağlantı `tests/manual/test_gemini_live.py` (varsayılan atlanır).
- Anahtar yoksa agent “devre dışı”, mühendislik UI'si çalışır. Anahtar yokken de çalışan **deterministik “Bu tesisi analiz et” pipeline'ı** (`services.analyze_facility`) vardır; Gemini yalnız narration ekler.

## 6. Simülasyon/Optimizasyon dürüstlüğü

- Modellenmeyenler (`SimResult.not_modelled`) açıkça belirtilir; modellenmiş gibi gösterilmez.
- CRN: varış ve işlem süresi RNG'leri ayrı `numpy.random.Generator(seed)`; senaryolar aynı seed.
- Tasarruf/ROI: `Project.cost is None` ise `None` döner.
- Rapordaki her sayı `build_report_context` içinde nesnelerden okunur; QA testi rapor↔hesap tutarlılığını doğrular.
- Optimizasyon yalnız `validate_layout` hatasız sonuçları önerir.

## 7. Entegrasyon sırası ve kapılar

Her dalga sonu Master: (1) worktree'leri merge, (2) `ruff check` + `pytest`, (3) bir sonraki dalgayı başlat. Kırmızı test varsa dalga kapanmaz.
