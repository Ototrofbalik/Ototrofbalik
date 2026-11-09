"""[Backend Engineer] Uygulama servis katmanı: Project/Layout/Run kalıcılığı + modülleri orkestre eden
tek giriş noktası. API (FastAPI), Streamlit ve Agent tool'ları YALNIZ buradan çağırır.

Beklenen (Backend Engineer'ın uygulayacağı) yüzey:
  get_project(pid) / save_project(p) / list_projects() / ensure_demo_project()
  save_layout_version(pid, layout) / list_layout_versions(pid)
  record_simulation_run(pid, SimResult) / get_latest_sim(pid, layout_id)
  save_optimization(pid, OptimizationResult)
"""
