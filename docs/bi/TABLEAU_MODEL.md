# Tableau specification — Output 6 Logistics and Network Design

**What exists:** the same star-schema extracts as Power BI (`outputs/logistics/bi/*.csv|parquet`) and the calculated fields below.
**What does not exist:** a `.twb`/`.twbx` workbook — author it in Tableau Desktop from these assets.

## Data source
Use **relationships** (not joins) in the logical layer, mirroring `POWER_BI_MODEL.md`: facts relate to `dim_city` on `city_id`, to `dim_date` on `date_key`, to `dim_zone` on `zone`, to `dim_address` on `address_id`, to `dim_depot` on `depot_id`, to `dim_vehicle_type` on `vehicle_type`. Add a parameter **WACC** (float, 0.08–0.16, default 0.12) and a parameter **Plan** (string: baseline / optimized).

## Calculated fields
```
// Routing
[Is On Time]                 = IF [On Time] THEN 1 ELSE 0 END
[Last Mile Cost]             = SUM([Cost Total]) + SUM([Outsourcing Cost])
[Baseline Cost]              = SUM(IF [Plan] = "baseline" THEN [Cost Total] END)
[Optimized Cost]             = SUM(IF [Plan] = "optimized" THEN [Cost Total] END)
[Fleet Cost Reduction %]     = ([Baseline Cost] - [Optimized Cost]) / [Baseline Cost]
[On-Time %]                  = SUM([Is On Time]) / COUNT([Order Id])
[Lateness Band]              = IF [Late Min] = 0 THEN "On time" ELSEIF [Late Min] <= 15 THEN "<= 15 min"
                               ELSEIF [Late Min] <= 60 THEN "15-60 min" ELSE "> 60 min" END
[Road vs Straight Ratio]     = SUM([Leg Road Km]) / SUM([Leg Straight Km])

// Fleet
[Lease Preferred]            = IF SUM([Tco Lease]) < SUM([Tco Own]) THEN "Lease" ELSE "Own" END
[Fleet Gap vs Current]       = SUM([Recommended Fleet]) - SUM([Current Fleet Size])

// Warehouse
[Picks per Hour]             = SUM([Pick Lines]) / SUM([Total Hours])
[Travel m per Line]          = SUM([Travel M]) / SUM([Pick Lines])

// Simulation
[Meets 2h Target]            = IF MAX([Pooled Cycle P95]) <= 120 THEN "Meets" ELSE "Misses" END

// Network & finance
[Discounted Cash Flow]       = [Cash Flow] / POWER(1 + [WACC], [Year])
[NPV at WACC]                = SUM([Discounted Cash Flow])
[Payback Flag]               = IF RUNNING_SUM(SUM([Cash Flow])) >= 0 THEN "Paid back" ELSE "Not yet" END
```

## Level-of-detail expressions
```
[City Baseline Cost]         = { FIXED [City Id] : SUM(IF [Plan] = "baseline" THEN [Cost Total] END) }
[City Optimized Cost]        = { FIXED [City Id] : SUM(IF [Plan] = "optimized" THEN [Cost Total] END) }
[City Cost Reduction %]      = ([City Baseline Cost] - [City Optimized Cost]) / [City Baseline Cost]
[Zone On-Time vs City]       = [On-Time %] - { FIXED [City Id], [Plan] : SUM([Is On Time]) / COUNT([Order Id]) }
[Best NPV per City]          = { FIXED [City Id] : MAX([Npv]) }
[Is Best Configuration]      = [Npv] = [Best NPV per City]
[Peak Day Orders per City]   = { FIXED [City Id], [Date Key] : COUNT([Order Id]) }
```

## Dashboards (one per module, same palette as the portfolio: ink #1F2A44, teal #0E7C86, marigold #E3A21A, slate #6B7A90, signal #C2413B)
1. **Command Center** — KPI BANs, cost by city (plan colour), alerts table, city filter action to all sheets.
2. **Routes** — map (lat/lon, path by `Seq`, colour by vehicle), on-time by zone, lateness band histogram.
3. **Fleet** — fleet vs demand level lines, compliance with 95% reference line, TCO stacked bars.
4. **Warehouse** — aisle × bay highlight table, picks/hour by layout.
5. **Simulation** — P95 bars with 120-min reference line, box plot of `Throughput` by scenario from fact_simulation_runs.
6. **Network & NPV** — site map, km-vs-p and SLA-vs-p lines, NPV at WACC parameter, break-even table.
