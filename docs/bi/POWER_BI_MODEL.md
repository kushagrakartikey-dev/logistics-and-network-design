# Power BI model specification — Output 6 Logistics and Network Design

**What exists:** import-ready tables in `outputs/logistics/bi/` (CSV + Parquet, 25 tables: 8 dimensions, 17 facts) and this specification with every relationship and DAX measure (`measures.dax`).
**What does not exist:** a `.pbix` file. Power BI Desktop files cannot be authored or validated in this build environment; build the report from these assets (≈30 minutes) and save the `.pbix` alongside this file.

## 1. Load
Get Data → Text/CSV (or Parquet) → select every file in `outputs/logistics/bi/`. Set `date` columns to Date, `*_key` to Whole number, booleans to True/False.

## 2. Star schema

| Table | Type | Grain | Key |
|---|---|---|---|
| dim_city | dim | metro cluster | city_id |
| dim_date | dim | calendar day | date_key |
| dim_zone | dim | delivery zone | zone |
| dim_depot | dim | depot | depot_id |
| dim_vehicle | dim | vehicle (current fleet) | vehicle_id |
| dim_vehicle_type | dim | vehicle type | vehicle_type |
| dim_address | dim | delivery address | address_id |
| dim_site | dim | candidate MFC site | site_id |
| fact_orders | fact | order | order_id |
| fact_city_day | fact | city × day | city_id + date_key |
| fact_routes | fact | plan × route | plan + vehicle_id |
| fact_route_stops | fact | plan × stop | plan + order_id |
| fact_unassigned | fact | plan × unserved order | plan + order_id |
| fact_fleet_scenarios | fact | city (or ALL) × demand level | city_id + demand_multiplier |
| fact_fleet_tco | fact | city × level × vehicle type | composite |
| fact_warehouse_performance | fact | city DC × layout | city_id + layout |
| fact_warehouse_heatmap | fact | city DC × aisle × bay | composite |
| fact_simulation_runs | fact | city × scenario × replication | composite |
| fact_simulation_summary | fact | city × scenario | city_id + scenario |
| fact_network_scenarios | fact | city × p | city_id + p |
| fact_network_cashflows | fact | city × p × year | composite |
| fact_network_assignments | fact | city × p × address | composite |
| fact_ml_performance / fact_ml_demand_validation / fact_cc_alerts | fact | see manifest | — |

`bi_manifest.json` lists row counts and columns for every table.

## 3. Relationships (single direction, many-to-one, dimension filters fact)

| From (many) | To (one) |
|---|---|
| fact_orders[date_key], fact_city_day[date_key], fact_ml_demand_validation[date_key] | dim_date[date_key] |
| every fact[city_id] (except fact_fleet_scenarios, which has an `ALL` row — filter it out with a page filter `city_id <> "ALL"` or keep it unrelated) | dim_city[city_id] |
| fact_orders[address_id], fact_network_assignments[address_id] | dim_address[address_id] |
| fact_orders[depot_id], fact_routes[depot_id] | dim_depot[depot_id] |
| fact_orders[zone], fact_route_stops[zone] | dim_zone[zone] |
| fact_routes[vehicle_type], fact_fleet_tco[vehicle_type] | dim_vehicle_type[vehicle_type] |
| dim_vehicle[vehicle_type] | dim_vehicle_type[vehicle_type] |

Create a disconnected table `Plan = DATATABLE("plan", STRING, {{"baseline"},{"optimized"}})` for slicers, and a What-if parameter **WACC** (0.08–0.16, step 0.01, default 0.12).

## 4. Pages
1. **Command Center** — cards: Health Score, Last-Mile Cost Reduction %, On-Time %, Vehicles Saved, Pick Travel Reduction %, Throughput Gain %; clustered bar of cost by city (plan legend); alert table (fact_cc_alerts) with severity conditional formatting; city slicer.
2. **Route Optimization** — Azure/ArcGIS map of fact_route_stops (lat/lon, legend vehicle_id, plan slicer); on-time % by zone bar; route table with load/duty utilisation data bars; unassigned orders table.
3. **Fleet Sizing** — line: Recommended Fleet by demand_multiplier (legend city); line: current-fleet compliance with constant line 95%; stacked bar TCO components; own vs lease.
4. **Warehouse** — matrix heatmap fact_warehouse_heatmap (rows bay, columns aisle, values optimized_picks, background colour scale), layout slicer; clustered bar picks/hour by layout; KPI cards travel reduction and bound.
5. **Simulation** — clustered bar P95 cycle by scenario with 120-min constant line; box-style P10/P50/P90 from fact_simulation_runs; utilisation matrix.
6. **Network Design & NPV** — map of dim_site sized by selected; line avg km vs p; SLA coverage vs p; NPV (WACC what-if) by city and p; break-even table.
7. **Financial Analysis** — waterfall of five-year cash flow (fact_network_cashflows), fleet TCO by city, delivered cost per order.

## 5. Refresh
Re-run `python run_demo.py --modules all` (regenerates data, models and these tables with the same seed), then **Home → Refresh**. Folder-based Parquet sources make refresh schema-stable.
