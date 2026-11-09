# Simülasyon Metodolojisi (SimPy)

Kod: `backend/simulation/engine.py`, `backend/analytics/bottleneck.py`. Birimler: metre, saniye.

## 1. Model

- **Varlık = parti (lot).** `Product.arrival_rate_per_hour` birim/saat; parti varış hızı `rate / batch_size`. İşlem süreleri parti başınadır. `completed` ve `throughput_per_hour` birim cinsindendir (parti x batch_size); WIP ve çevrim süresi parti bazlıdır.
- **İstasyon:** `simpy.Resource(capacity)`; FIFO kuyruk. İşlem süresi = `RouteStep` değeri, yoksa `Workstation` değeri; hazırlık (setup) her işlemde bir kez uygulanır (sıra bağımlı değil).
- **Kuyruk kapasitesi (`queue_capacity`):** `capacity + queue_capacity` büyüklüğünde ek bir kabul kaynağı ile modellenir. Sonraki istasyonda yer yoksa varlık *mevcut istasyonu tutarak* bekler (blokaj); bu süre kuyruk süresine yazılır. Taşımadaki varlık da hedefte yer rezerve eder. Döngüsel rota + dar kuyruk kilitlenmeye (deadlock) yol açabilir; uyarı verilmez.
- **Operatör:** `requires_operator` istasyonlar işlem (hazırlık dahil) boyunca havuzdan bir operatör tutar; makineyi önce alır, sonra operatörü bekler (bu bekleme *kaynak bekleme*dir, makine bu sürede dolu sayılır ama "kullanım"a girmez). Operatör taşımaları **aynı havuzu** kullanır.
- **Taşıma:** her geçiş için `transport_in` araç havuzundan (kimlikli `simpy.Store`, ör. `forklift-1`) bir araç alınır; süre = `mesafe / hız + yükleme + boşaltma`. Mesafe yalnız `RouteSet.get(from, to, vehicle).distance_m`'den okunur. Rota yok/`reachable=False`, araç kaynağı tanımsız/0, `RouteSet.layout_id` uyuşmazlığı, bilinmeyen istasyon => **simülasyon başlamadan** `RouteUnavailableError` / `SimulationError` (ikisi `ValueError`); sessizce 0 kullanılmaz. Aynı istasyona ardışık adım: taşıma yok.
- **Varış:** `poisson` (üstel aralık) veya `deterministic` (aralık sabit, ilk varış t=0). `max_wip` aşılırsa varış reddedilir (kayıp) ve notes'a yazılır.
- **İşlem değişkenliği:** `deterministic` veya `triangular...` (çarpan ~ Triangular(0.8, 1.0, 1.2), ortalama 1).
- **Olay logu:** ENTITY_CREATED, QUEUE_ENTERED (her istasyon varışında; `queue_len` = kuyruktaki bekleyen sayısı, kendisi dahil bekliyorsa), PROCESS_STARTED/FINISHED (hazırlık+işlem), TRANSPORT_REQUESTED, TRANSPORT_STARTED (araç tahsis edildi, yükleme başlar), TRANSPORT_COMPLETED (boşaltma bitti), ENTITY_COMPLETED. Yalnız replikasyon 0; en çok 100 000 kayıt, aşılırsa kesilir ve `notes`'a yazılır (KPI'lar etkilenmez).

## 2. Rastgelelik ve CRN

Her ürün için iki bağımsız `numpy.random.Generator`: varış `SeedSequence([seed, 0, ürün_idx])`, işlem süresi çarpanları `SeedSequence([seed, 1, ürün_idx])`. İşlem çarpanları varlık oluşturulurken çekilir; böylece akış rota/yerleşim/kaynak sayısından bağımsızdır: aynı seed => iki senaryoda aynı varış zamanları ve aynı işlem süreleri. Replikasyon `i` için seed = `config.seed + i`.

## 3. Ölçüm penceresi ve KPI'lar

Simülasyon `duration_s`'ye kadar koşar; istatistikler `[warmup_s, duration_s]` penceresinde toplanır (`warmup_s < duration_s`).

- **Throughput:** pencerede tamamlanan birim / pencere saati. **Cycle time** = tamamlanma - oluşturulma (sistemde kalış), pencerede tamamlananların ortalaması. **Lead time = cycle time** (siparişe-serbest bırakma gecikmesi modellenmedi).
- **WIP:** sistemdeki parti sayısının zaman-ağırlıklı ortalaması (integral / pencere). Little yasası `L = λW` testle doğrulanır.
- **Utilization:** istasyon = meşgul sunucu-saniyesi / (capacity x pencere) (yalnız hazırlık+işlem). Araç türü = tutulan araç-saniyesi / (count x pencere); operatör kullanımı işlem + taşıma (yükleme/boşaltma dahil) süresini içerir.
- **Zaman ayrışımı (ürün başına ortalama):** işlem, hazırlık, taşıma (seyir), yükleme/boşaltma, kaynak bekleme (araç + operatör), kuyruk (istasyon kuyruğu + blokaj/kabul beklemesi). Toplamı **tam olarak** cycle time'a eşittir (test: `rel=1e-9`).
- `avg_queue_wait_s`: istasyon ziyareti başına ortalama kuyruk bekleme (blokaj hariç). `avg_queue_s`: varlık başına toplam kuyruk süresi (blokaj dahil). `max_queue_len`: pencerede görülen en büyük kuyruk.
- Mesafe/seyir süresi, bacağın seyir aralığının pencereyle kesişimi oranında sayılır.

## 4. Replikasyon ve güven aralığı

`replications = n` => seed, seed+1, ... Ana KPI'lar replikasyon ortalamasıdır (`completed` yuvarlanmış ortalama, `max_queue_len` replikasyonlar arası maksimum). `ci95[kpi]` = ortalama ± t(0.975, n-1) x s / sqrt(n) (scipy); n=1 ise boş. Tüm `SimKPIs` alanları için verilir.

## 5. Darboğaz ve karşılaştırma

`analyze_bottlenecks`: skor = 0.5 x kullanım + 0.3 x (ort. kuyruk bekleme / en büyük) + 0.2 x (maks. kuyruk / en büyük); en yüksek skorlu istasyon, kullanımı >= %5 ise `bottleneck_station_id`. Ayrıca kaynak (operatör/forklift) doygunluğu (>= %85) ve süre ayrışımı payları raporlanır. Darboğaz *istasyon* bazlıdır; ortak operatör havuzu kısıtı `resource_constraints` altında ayrıca görünür.

`compare_results`: her KPI için `KpiDelta` (mutlak, yüzde, `better`: verim/tamamlanan yüksek iyi; süre, WIP, kuyruk, mesafe düşük iyi; işlem/hazırlık süresi için yön belirsiz = `None`). Opsiyonel `FlowMetrics` deltaları `flow.*`. `same_seed` = iki sonucun seed'i eşit. **Tasarruf yalnız `project.cost` varsa** hesaplanır: taşıma mesafesi (m/saat) x `cost_per_meter` farkı; işgücü maliyeti dahil değildir.

## 6. Modellenmeyenler (`not_modelled`)

arıza, trafik çakışması / koridor kapasitesi, vardiya, **boş araç seyri** (`empty_travel_m = 0`: araçlar yüklü seyir dışında hareket etmez; boşta konumlanma yok), öncelik/dağıtım kuralları (FIFO). Ayrıca: sıra bağımlı hazırlık, ürün karması önceliği, operatörün istasyonlar arası yürüme süresi (operatör taşıması yalnız `transport_in=operator` adımlarında).
Rota modu `network` değilse (`euclidean`/`manhattan`) sonuç notlarında mesafelerin yaklaşık olduğu belirtilir.
