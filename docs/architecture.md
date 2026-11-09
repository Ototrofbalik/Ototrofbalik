# Mimari Özeti
Ayrıntı için kök dizindeki `ARCHITECTURE.md`.

```
Streamlit ─┐                         ┌─ layout (doğrulama/düzenleme)
FastAPI  ──┼─▶ backend/services.py ─▶├─ routing (koridor grafı, A*) ─▶ spaghetti
Agent tools┘        │                ├─ simulation (SimPy) + analytics
                    ▼                ├─ optimization (SA/local/swap/OR-Tools)
               SQLite (SQLAlchemy)   └─ reporting (HTML/PDF/XLSX/PNG)
backend/pipeline.py : analyze_facility (Gemini'siz uçtan uca)
```
Kurallar: hesap yalnız domain modüllerinde; modüller yalnız `backend/schemas` tipleriyle konuşur; UI/API/Agent yalnız `services`/`pipeline` kullanır; aynı `RouteSet` hem Spaghetti hem SimPy'de kullanılır; layout değiştiren agent tool'ları onay ister.
