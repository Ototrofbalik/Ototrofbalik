# Veri Modeli
Tek doğruluk kaynağı: `backend/schemas/`. Birimler: metre, saniye; orijin sol-alt.

- **Layout**: Facility, Workstation (konum, ebat, rotasyon, giriş/çıkış ofseti, süreç parametreleri), Corridor (polyline, genişlik, tek yön, araç izni), Obstacle.
- **Akış**: Product → RouteStep; FlowEdge (From-To); Route/RouteSet (ağ mesafesi, `mode` network/euclidean/manhattan); FlowMetrics.
- **Simülasyon**: SimConfig, ResourceConfig, CostConfig (opsiyonel), EventRecord, SimKPIs, SimResult (CI, not_modelled).
- **Optimizasyon**: OptimizationConfig, LayoutAlternative, OptimizationResult, ScenarioComparison.
- **Agent**: ToolResult, PendingChange, ToolCallRecord, GeminiStatus, AgentReply.
- **Veritabanı** (SQLAlchemy, SQLite varsayılan): projects, facilities, layouts, workstations, corridors, obstacles, products, process_routes, simulation_scenarios, simulation_runs, movement_events, simulation_metrics, optimization_results, agent_tasks, reports. Layout sürümlenir (`version`, `parent_id`); eski analizler silinmez.
