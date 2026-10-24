# Methodology — Output 6: Logistics and Network Design

Technical reference for engineering and analytics interviews. Every parameter cited here lives in `config/logistics.yaml` and is listed with its unit and rationale in the master data register (Excel sheet 1).

## 1. Scenario and data

**Geography.** Five metro clusters, each with three depots (outskirts DC, existing MFC, cross-dock hub), 500 delivery addresses scattered around real locality anchors in five zones, a 25-vehicle fleet and candidate MFC sites (15 in Mumbai, 10 elsewhere).

| Cluster | States / UTs | Volume weight | Congestion index | Wage index |
|---|---|---|---|---|
| Mumbai MMR | Maharashtra | 1.00 | 1.00 | 1.00 |
| Delhi NCR | Delhi, Haryana, Uttar Pradesh | 1.05 | 0.95 | 1.00 |
| Bengaluru | Karnataka | 0.85 | 1.12 | 1.02 |
| Chennai | Tamil Nadu | 0.70 | 0.92 | 0.92 |
| Kolkata | West Bengal | 0.60 | 1.05 | 0.85 |

**Calendar (1 Sep – 29 Nov 2025).** National events (festive sale wave, Navratri dealer stocking, Dussehra, Diwali peak 10–19 Oct, Diwali holidays) and regional events applied only in their city: Ganesh Visarjan (Mumbai), Durga Puja (Kolkata), Chhath Puja (Delhi NCR), Ayudha Puja (Bengaluru, Chennai), Kannada Rajyotsava (Bengaluru). Weekday factors differ for B2C and B2B. Rain-day probabilities follow each city's monsoon pattern (Chennai's north-east monsoon peaks Oct–Nov).

**Orders.** Poisson daily counts per city × segment (e-commerce B2C, FMCG retail, modern retail, automotive) scaled by weekday and event multipliers; lognormal weights per segment; Poisson order lines; segment-specific delivery windows (2-hour express windows for B2C and JIT parts); service time 4 min (B2C) or 10 min + 0.035 min/kg (B2B). **Stocking policy:** each order ships from the nearest depot in its city that stocks its segment (B2C at all three; FMCG at the hub; modern retail and automotive at the DC).

**Telematics trip logs** record observed leg times generated from zone circuity (road/straight-line ratio), the city congestion index, rain, event traffic, vehicle behaviour and lognormal noise. These logs are the only place zone-level road effects appear; the routing planner does not know them unless the ML model learns them.

**Validation.** Every table passes a data contract (`src/shared/validation.py`: types, nulls, ranges, allowed values, unique keys), foreign-key checks, and business rules (addresses inside the city bounding box, 25 vehicles per city, no cross-city fulfilment, windows inside depot hours, heavy SKUs ≤ floor/lower-shelf slots). Generation is deterministic (seeded `SeedSequence` streams).

## 2. Distance and travel time

* **Straight-line km:** Haversine (mean Earth radius 6,371.0088 km; matches `geopy.great_circle`).
* **Road km (estimate):** straight-line × 1.35 planning circuity. No street-network routing is performed; road km is labelled as an estimate everywhere.
* **Travel minutes:** road km / vehicle free-flow speed × 60 × clock-time traffic band (1.0 before 08:00, **1.8** 08:00–11:00, **1.1** 11:00–17:00, **2.2** 17:00–21:00) × ML zone calibration factor.

## 3. Vehicle routing (CVRPTW) — `vrp_optimizer.py`

**Decomposition.** Orders are pinned to a stocking depot and vehicles are depot-based, so the multi-depot problem splits exactly into one CVRPTW per depot (15 depots).

**OR-Tools model.** Nodes = depot + orders; per-vehicle-type transit matrices; time dimension in 0.1-minute units with 240-minute waiting slack; hard windows on every order; duty span limit per vehicle with a 6% planning buffer; capacity dimension in kg; **eligibility** via `VehicleVar.SetValues` (capacity, 15 kg single-parcel limit for 2-wheelers, 2-ton vans barred from each city's core business district); every order is an optional node with a large penalty, so infeasible orders are dropped and *reported*, not silently lost. Objective (configurable `cost | distance | time`): ₹/km arc costs + driver wage as span cost + fixed daily vehicle cost. Search: `PATH_CHEAPEST_ARC` → `GUIDED_LOCAL_SEARCH`, time limit scaled to depot size (≤ 30 s).

**Time dependence.** Arc times in the solver use the slower traffic band of the arc's origin and destination window midpoints (conservative planning). The **evaluator** then replays every route with the traffic band at the actual departure minute — this is what is reported, so the solver's simplification cannot inflate results. Lateness and duty overruns found on replay are reported.

**Baseline.** Window-aware nearest-neighbour dispatch (Solomon-style): each vehicle repeatedly takes the eligible order with the smallest *effective distance* (road km + waiting time expressed in km) among orders it can still reach before the window closes; a second pass appends remaining orders as late deliveries; any remainder is unassigned. An earlier window-blind baseline was rejected because it inflated savings through outsourcing.

**Costs.** Last-mile cost = Σ route (fixed + km × ₹/km + duty hours × wage) + spot-outsourcing cost of unserved orders. Fleet cost excludes outsourcing. Both are reported.

## 4. Fleet sizing — `fleet_sizer.py`

For demand multipliers 0.5×–3.0× of the representative day (orders sub-sampled below 1.0×; above 1.0× extra orders bootstrapped from non-event weekdays with the same city/segment mix):
1. Route the **current** 25 vehicles per city → compliance, unserved, cost.
2. Route a **candidate pool** per depot and type (current × max(1, m) × 1.6, at least 2) with each vehicle's fixed daily cost in the objective — a fleet-size-and-mix VRP. Vehicles actually used = recommended fleet and mix.
3. Feasible iff compliance > 95%; infeasible levels are reported, never relaxed.
4. **TCO:** ownership = equivalent annual cost `(capex − residual/(1+r)^n) × r/(1−(1+r)^−n)` + insurance + maintenance + fuel (72% of ₹/km) + labour at city wage; lease = lease × 12 × (1 + 3% admin) + fuel + labour.

The validated 14-day demand forecast maps onto the nearest covering demand level for planning.

## 5. Warehouse slotting — `warehouse_slotter.py`

**Layout:** 50 aisles × 2 sides × 20 bays × 5 levels = 10,000 slots per city DC; dispatch at the front-left; zones A/B/C = nearest 2,000 / next 4,000 / last 4,000 slots by rectilinear distance; levels 1–2 are floor/approved lower shelf.

**Method:** (1) SKU velocity from the training two-thirds of each city's pick history, with empirical-Bayes shrinkage toward the product-family mean (weight chosen by internal 75/25 time validation inside the training window); (2) co-pick graph (NetworkX) clustered with Louvain; (3) velocity ranking fills zones A→B→C; within a zone, clusters are placed contiguously along the walking sequence, light fast movers prefer waist-height levels 2–3; (4) **hard constraint:** SKUs > 10 kg only on levels 1–2.

**Evaluation (out-of-sample):** held-out last third of pick orders, batches of 3, return routing (enter each required aisle to the deepest bay and back), walking 0.9 m/s, 9 s handling per line + 7 s at levels 4–5 + 12 s for heavy items above approved levels, 60 s tour setup. **Baseline:** category-code order (current practice). **Perfect-information bound:** the same method fed each SKU's long-run demand rate — separates method quality from data limits.

## 6. DC simulation — `simulation_engine.py`

SimPy model per city DC, 06:00–22:00, two shifts: intake → wave release (30-minute waves) → pick (Normal(45 s, 10 s) per line + walking time per line from the optimized slotting) → pack (Uniform(3, 8) min + 0.2 min per extra line) → QC (5% sample, Normal(4, 1) min) → dispatch (Uniform(2, 6) min) → delivery leg sampled from the optimized routing plan. Order count Poisson; intake times and lines resampled from matching days (normal weekdays vs Diwali peak days).

**Scenarios (200 replications each):** baseline roster (scaled by city volume); **optimized roster** — bottleneck-driven search (grow the most-utilised role until pooled P95 ≤ 120 min and ≥ 99.5% of orders dispatched in-day, then trim the most expensive roles), **verified at 200 replications** with heads added at the bottleneck until it truly passes; Diwali peak at baseline roster; Diwali peak with a peak roster found the same way. Reported: P10/P50/P90 across replications of throughput and P95 cycle, pooled P95, SLA share, backlog, utilisation, queue waits and the bottleneck.

## 7. Network design — `network_designer.py`

**p-median** per city (PuLP): existing three facilities stay open; choose p ∈ {1..5} new MFCs minimising demand-weighted road distance for MFC-eligible (B2C parcel) demand; **binary single-sourcing** assignment; MFC capacity 450 parcels/day. Solver: HiGHS, or CP-SAT when OR-Tools is loaded in the same process (all variables are integer, so CP-SAT is exact here; `src/shared/solvers.py` refuses CP-SAT for models with continuous variables because it rounds them).

**Economics.** Variable last-mile cost per order-km is calibrated per city from the optimized routing plan (km cost + driver time ÷ Σ depot-to-stop road km). Annual saving = distance reduction × that rate × 310 days. Costs: capex ₹65 lakh per MFC (year 0), rent (site-specific ₹/sqft) + staff (wage-indexed) + utilities, line-haul from the city DC (2 trucks/day × ₹32/km). NPV over 5 years at 12% WACC with 18% parcel growth, no terminal value. **Break-even volume** = PV(costs) / PV(savings at today's volume). **2-hour SLA** coverage = share of parcel demand whose travel time at evening-peak traffic (2.2×) plus 40 minutes processing is ≤ 120 minutes. One-way sensitivity on WACC, capex, growth and variable cost.

## 8. Machine learning — `ml_models.py`

**Split:** the last 21 days are validation; models never see them.

* **Travel time.** Target: log(observed minutes / planning-formula minutes). Gradient boosting (300 trees, depth 3, learning rate 0.05, subsample 0.85) on straight-line km, departure hour, traffic band, rain, event traffic, weekday, city, origin/destination zone and vehicle type. Baseline: the planning formula. If the model wins on MAE, per-zone calibration factors (median predicted ratio under dry, non-event conditions) are published and used by routing, fleet sizing and the simulation's delivery legs.
* **Zone demand.** Daily orders per zone from features known in advance (weekday, published event calendar, days to Diwali, trend, zone, city) — a direct multi-horizon model with no lagged actuals, so there is no leakage. Baseline: seasonal naive (last training week repeated). The 14-day forecast feeds fleet scenario selection and Command Center workload only if it beats the baseline.

Metrics: MAE, RMSE, MAPE.

## 9. Command Center integration — `integration.py`

Publishes `outputs/command_center/logistics_contract.json` (shared envelope in `src/shared/command_center.py`) with: lane transit/lateness statistics and σ_LT for Output 5's safety-stock formula; the change in the lead-time variance term (baseline → optimized); delivered cost per order/kg by city for Output 4's TCO; DC picks/hour; 14-day workload (orders, lines, picker-hours); MFC ranking. Upstream contracts are read when present; when Output 5 has not run, safety-stock impact is reported as "awaiting upstream" instead of being estimated. **Health score (0–100)** = 30 × delivery (0 at 90% in-window, 100 at 99%, scaled down by the share of routes with duty overruns) + 20 × min(fleet-cost reduction / 22%, 1) + 15 × min(throughput gain / 30%, 1) + 20 × DC readiness (mean of cities meeting P95 with the optimized roster and cities meeting it at Diwali with today's roster) + 15 × share of city demand levels the *current* fleet serves at target. It is built to expose risk, not to reward hitting targets.

## 10. Reproducibility and limits

* Fixed seeds everywhere; OR-Tools searches are wall-clock limited, so results can shift slightly with machine load (KPIs typically within ±1 pt).
* Road distances are estimates (circuity), not street routing; traffic is banded, not live.
* The SimPy model treats each order as one pick task (no batch/zone picking) and constant staffing across the two shifts.
* Network economics exclude revenue effects of faster delivery and any terminal value — conservative by design.
* OR-Tools and `highspy` cannot be imported into one process in either order without conflict; see `src/shared/solvers.py`.
