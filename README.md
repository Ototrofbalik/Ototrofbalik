# FactoryFlow AI

Üretim/depo yerleşimi tasarlayan, fiziksel hareket akışını **SimPy** ile simüle eden, **Spaghetti Chart** üreten, yerleşimi **optimize eden**, mühendislik raporu hazırlayan ve **Gemini AI Agent** ile doğal dilde yönetilebilen yerel bir mühendislik platformu.

> Demo veri **örnek / varsayımsaldır**. Gemini KPI hesaplamaz; gerçek Python modüllerini tool olarak çağırır ve sonuçları yorumlar. Gemini anahtarı yoksa tüm mühendislik modülleri ve çevrimdışı "Bu tesisi analiz et" akışı yine çalışır.

## Gereksinimler
Python 3.12+ (3.13 ile denendi). Ücretli bulut servisi gerekmez.

## Kurulum
```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

## Çalıştırma
```bash
uvicorn backend.main:app --reload          # API  (http://localhost:8000/docs)
streamlit run frontend/streamlit_app.py    # Arayüz
```
Arayüz doğrudan `backend.services` modülünü kullanır; API ayrıca dış istemciler içindir.

## Gemini API anahtarı ve model seçimi
`.env` içine yaz (dosya `.gitignore`'dadır; anahtar koda, loglara veya raporlara yazılmaz):
```dotenv
GEMINI_API_KEY=...
GEMINI_MODEL=...          # kendi hesabında KULLANABİLDİĞİN bir model; kod içinde sabit model yoktur
GEMINI_ENABLED=true
GEMINI_MAX_REQUESTS_PER_TASK=10
GEMINI_DAILY_REQUEST_BUDGET=100
```
Model adlarının ömrü kısadır: ör. `gemini-2.5-flash` yeni kullanıcılara kapatılmıştı (404); bu oturumda `gemini-3.5-flash` ve `gemini-3.1-flash-lite` çalıştı. Güncel listeyi Settings sayfasından veya `GeminiClient().list_available_models()` ile gör, 404 alırsan başka model seç. Ücretsiz model/limitler değişebilir: güncel liste için Google AI Studio dokümantasyonuna bak; ücretsiz kota garanti edilmez. Uygulama ücretli modele **otomatik geçmez**. Kota aşılırsa AI kilitlenir, mühendislik uygulaması çalışmaya devam eder.
Tesis verisi Gemini'ye yalnız `.env` içinde `EXTERNAL_DATA_CONSENT=true` yaptığında gönderilir (Settings sayfası durumu gösterir, arayüzden değiştirilmez); varsayılan `false` olduğundan veri yerelde kalır ve çevrimdışı mod kullanılır.
Gerçek bağlantıyı bir kez sınamak için: `RUN_GEMINI_LIVE=1 pytest tests/manual/test_gemini_live.py`

## Kullanım
- **Demo verisi:** Arayüzde "Demo yükle" (30×20 m, 7 istasyon, 2 operatör, 1 forklift).
- **Layout düzenleme:** *Layout Designer* sayfası; koordinat girişiyle istasyon ekle/taşı/döndür/sil. Çakışma ve ihlaller kırmızı gösterilir; geçersiz taslak kaydedilmez (veya açık onay ister).
- **Spaghetti Chart:** *Spaghetti Analysis* sayfası; ağ rotaları koridor grafı üzerinde A* ile bulunur. Araç filtresi, ısı haritası, önce/sonra.
- **Simülasyon:** üst çubukta *Run Simulation* (varsayılan 8 saat, seed 42). Olay logu, KPI'lar, darboğaz.
- **Optimizasyon:** *Optimize* (swap, local search, simulated annealing, OR-Tools); yalnız `validate_layout`'tan geçen ≥3 alternatif, aynı seed ile simüle edilir.
- **Rapor:** *Reports* sayfası; HTML, PDF, Excel, PNG.
- **AI:** sağ panele yaz, örn. *"Bu tesisi analiz et"*. Layout değiştiren öneriler **onay bekler** (Onayla/Reddet).

## Testler
```bash
pytest tests            # tam paket ~5 dk (optimizasyon testleri yavaştır)
pytest tests --deselect tests/test_optimization.py   # hızlı
ruff check .
```
Gemini testleri gerçek API kullanmaz (`tests/fakes/FakeGeminiClient`).

## Mimari
Bkz. `ARCHITECTURE.md`, `docs/architecture.md`, `docs/data_model.md`, `docs/simulation_methodology.md`.

## Sınırlamalar (dürüstçe)
Simülasyonda **modellenmeyenler**: arıza, trafik çakışması, vardiya, boş araç seyri (her sonuçta `not_modelled` ile belirtilir). Lead time şu an cycle time ile aynıdır. ROI hesaplanamaz (yatırım maliyeti şemada yok); tasarruf yalnız taşıma maliyetinden ve yalnız maliyet verisi girilmişse hesaplanır. Google Sheets/Drive yalnız arayüz olarak hazırdır (OAuth bağlı değil).
