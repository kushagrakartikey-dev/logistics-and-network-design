# Case study — Logistics and Network Design: a five-metro 3PL network

**Client situation.** A 3PL/4PL serves e-commerce, FMCG, modern-retail and automotive customers from 15 facilities in five metro clusters — Mumbai MMR (Maharashtra), Delhi NCR (Delhi, Haryana, Uttar Pradesh), Bengaluru (Karnataka), Chennai (Tamil Nadu) and Kolkata (West Bengal) — with 125 vehicles (25 per city) and about 1,160 orders a day, peaking at 2,206 during the Diwali build-up. Routes are planned by dispatchers, DC rosters are fixed, and the business is being pitched micro-fulfilment centres (MFCs) for 2-hour delivery.

**Questions.** (1) How much can planned routing save without hurting service? (2) How big a fleet does each city need as volume grows? (3) Can the DCs pick faster? (4) Will the DCs hold a 2-hour dispatch promise, including at Diwali? (5) Where, if anywhere, should MFCs be opened?

## Findings

**1. Routing: −29.8% fleet cost, 20 fewer vehicles a day, service held above 95%.**
On a representative weekday (10 Sep 2025, 1,109 orders), OR-Tools routing cut fleet operating cost from ₹4.75 lakh to ₹3.34 lakh a day (−29.8%) and total last-mile cost including spot outsourcing by 28.8%, using 79 vehicles instead of 99 and 28.6% fewer road-km. In-window delivery moved from 98.0% to 98.4% nationally, with the gain concentrated in Delhi NCR (92.9% → 96.6%), where the Gurugram DC is furthest from demand. In Bengaluru, Chennai and Kolkata the optimizer trades a little on-time performance for cost (Bengaluru 99.5% → 98.1%), still above the 95% standard. City savings range from 20.3% (Mumbai, already dense) to 45.3% (Kolkata, where the manual baseline drives long Howrah–Salt Lake loops). Eight optimized routes overrun driver duty under replayed traffic; these are reported as overtime.

**2. Fleet: the current 125 vehicles are oversized for today and undersized for the festive peak.**
At the representative day's volume, 79–87 vehicles meet the 95% standard at the same daily cost (₹3.45–3.47 lakh). Each city's current 25-vehicle fleet holds the standard only up to: Delhi NCR 0.75x, Bengaluru 1x, Mumbai MMR 1x, Chennai 2x, Kolkata 2x; nationally it slips to 92.3% at 1.5x and 67.9% at 3.0x (over 1,000 orders unserved). Recommended fleets grow to 126 at 1.5x, 170 at 2.0x and 277 at 3.0x, tempo-heavy throughout. Annual TCO of the 1.0x fleet: ₹8.04 cr owned vs ₹8.53 cr leased. Infeasible city-levels: Mumbai MMR at 3x (94.5%).

**3. Warehouses: −26.2% picker walking, +23.4% throughput, zero heavy-item violations.**
Velocity zoning with co-pick affinity, tested on held-out pick orders, cut walking 24.4–27.5% by city and raised picks per hour 22.1–24.4%; labour per 1,000 picks fell 18–20%. 615 heavy-item placements above the approved shelf height per DC fall to zero. The 35% walking target is not reachable here: even with perfect knowledge of each SKU's demand the same method reaches only 26–27%, so the remaining gap is the picking policy (return routing, small batches), not slotting.

**4. DC operations: today's rosters miss the 2-hour target in four of five cities; Diwali breaks all five.**
Pooled P95 intake-to-dispatch time with current rosters: Chennai 307 min (loading bottleneck), Mumbai 171, Kolkata 164, Delhi 146, Bengaluru 101 (meets). One extra packer and loader brings the four to 88–106 min. At Diwali volume the current rosters reach 351–543 min with 54–176 orders a day left undispatched; peak rosters (e.g. Mumbai and Delhi 7 pickers / 7 packers / 1 QC / 5 loaders) restore 87–115 min — every roster verified across 200 Monte Carlo replications.

**5. Network: defer MFCs; Delhi NCR is first in line.**
At today's parcel volumes (106–194 parcels a day per city) no MFC configuration has a positive five-year NPV at 12% WACC. Ranking by break-even volume: **Delhi NCR first** — one MFC at Mayapuri pays back at ~551 parcels/day (2.8× today) and today's network covers only 42% of Delhi parcel demand within 2 hours at evening-peak traffic; then Mumbai (Byculla, ~1,131/day), Kolkata (Park Circus, ~673/day but already 98% covered), Bengaluru (Hebbal) and Chennai (Kolathur, 11.4×).

**ML that earned its place.** A travel-time model trained on telematics cut validation error from 10.3 to 7.8 minutes per leg (−24%) and now calibrates routing by zone (Bengaluru zones run ~1.12× the planning estimate; Chennai's OMR corridor 0.85×). A calendar-aware demand model beat the seasonal-naive baseline by 25% on MAE and feeds fleet planning and DC workload.

## Recommendations

1. Roll out planned routing city by city, starting where savings and service both move (Delhi NCR, Kolkata); monitor duty overruns.
2. Add one packer and one loader per shift in Mumbai, Delhi, Chennai and Kolkata now; contract peak labour for the ten days before Diwali.
3. Re-slot all five DCs; separately pilot zone picking with S-shape routing to go beyond the slotting ceiling.
4. Right-size the base fleet toward ~80–90 vehicles at today's volume (owned/leased per the TCO comparison) and lease festive-season capacity rather than owning for the peak; Delhi NCR needs added capacity first (its fleet holds target only to 0.75x).
5. Revisit MFCs when Delhi NCR parcel volume approaches ~550 a day; track the 2-hour coverage gap there.

## Limits
Road distances are estimates (1.35 circuity); traffic is banded; NPV excludes revenue upside and terminal value; OR-Tools searches are time-limited. Details in `METHODOLOGY.md`.
