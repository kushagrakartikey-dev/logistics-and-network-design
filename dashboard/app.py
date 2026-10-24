"""Local Plotly Dash dashboard for Output 6 (runs on http://127.0.0.1:8050).

    python dashboard/app.py            # after: python run_demo.py --modules all --export-reports

Reads the same ``dashboard/static/data/payload.json`` as the static GitHub Pages
build, so both views always show identical numbers. Figure builders are plain
functions (``fig_*``) so they can be tested without a browser.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.shared.style import PALETTE, hexc  # noqa: E402

PAYLOAD = ROOT / "dashboard" / "static" / "data" / "payload.json"
C = {k: hexc(k) for k in PALETTE}
CITY_COLORS = ["#1F2A44", "#0E7C86", "#E3A21A", "#8E5A9B", "#C2413B"]
SCEN_COLORS = [C["slate"], C["teal"], C["signal"], C["marigold"]]
FONT = "Archivo, Segoe UI, Arial, sans-serif"


def load_payload(path: Path = PAYLOAD) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run: python -m src.logistics.reporting.payload")
    return json.loads(path.read_text(encoding="utf-8"))


def _layout(fig: go.Figure, **kw: Any) -> go.Figure:
    fig.update_layout(template="plotly_white", font={"family": FONT, "size": 12.5, "color": C["ink"]},
                      margin={"l": 56, "r": 16, "t": 30, "b": 44}, legend={"orientation": "h", "y": -0.2}, **kw)
    return fig


def city_name(P: dict[str, Any], cid: str) -> str:
    return next(c["name"] for c in P["meta"]["cities"] if c["id"] == cid)


# --------------------------------------------------------------------------- figures
def fig_cost_by_city(P: dict[str, Any], selected: str) -> go.Figure:
    ids = [c["id"] for c in P["meta"]["cities"]]
    names = [city_name(P, c) for c in ids]
    k = P["routing"]["kpis"]
    fig = go.Figure([go.Bar(name="Manual baseline", x=names, y=[k[c]["baseline"]["last_mile_cost"] for c in ids], marker_color=C["slate"]),
                     go.Bar(name="Optimized", x=names, y=[k[c]["optimized"]["last_mile_cost"] for c in ids],
                            marker_color=[C["marigold"] if c == selected else C["teal"] for c in ids])])
    return _layout(fig, barmode="group", title="Daily last-mile cost by city", yaxis_tickprefix="₹")


def fig_demand(P: dict[str, Any], selected: str) -> go.Figure:
    fig = go.Figure()
    for i, c in enumerate(P["meta"]["cities"]):
        fig.add_scatter(x=P["demand"]["dates"], y=P["demand"][c["id"]], name=c["name"], mode="lines",
                        line={"color": CITY_COLORS[i], "width": 3 if c["id"] == selected else 1.4},
                        opacity=1 if c["id"] == selected else 0.55)
    fig.add_vrect(x0="2025-10-10", x1="2025-10-19", fillcolor=C["marigold"], opacity=0.12, line_width=0,
                  annotation_text="Diwali peak")
    return _layout(fig, title="Orders per day")


def fig_route_map(P: dict[str, Any], city: str, plan: str) -> go.Figure:
    fig = go.Figure()
    palette = ["#0E7C86", "#E3A21A", "#1F2A44", "#8E5A9B", "#3C8D5A", "#C2413B", "#2F6DB5", "#B8860B"]
    for i, r in enumerate(P["routing"]["routes"][city][plan]):
        fig.add_trace(go.Scattermap(lat=[p[0] for p in r["pts"]], lon=[p[1] for p in r["pts"]], mode="lines+markers",
                                    line={"width": 2.5, "color": palette[i % 8]}, marker={"size": 6},
                                    name=f"{r['v']} ({r['type']})", hovertext=f"{r['stops']} stops, {r['km']} km, ₹{r['cost']:,.0f}"))
    dep = P["routing"]["depots"][city]
    fig.add_trace(go.Scattermap(lat=[d[3] for d in dep], lon=[d[4] for d in dep], mode="markers+text", text=[d[1] for d in dep],
                                marker={"size": 15, "color": C["ink"]}, name="Depots", textposition="top right"))
    un = P["routing"]["unassigned"][city][plan]
    if un:
        fig.add_trace(go.Scattermap(lat=[u[0] for u in un], lon=[u[1] for u in un], mode="markers", name="Unserved",
                                    marker={"size": 11, "color": C["signal"]}, hovertext=[u[2] for u in un]))
    center = next(c["center"] for c in P["meta"]["cities"] if c["id"] == city)
    fig.update_layout(map={"style": "carto-positron", "center": {"lat": center[0], "lon": center[1]}, "zoom": 9.6},
                      margin={"l": 0, "r": 0, "t": 0, "b": 0}, showlegend=False, height=520)
    return fig


def fig_zone_ontime(P: dict[str, Any], city: str, plan: str) -> go.Figure:
    z = [r for r in P["routing"]["zones"][city] if r[0] == plan]
    target = P["meta"]["compliance_target"]
    fig = go.Figure(go.Bar(y=[r[1] for r in z], x=[r[2] for r in z], orientation="h", text=[f"{r[2]:.1%}" for r in z],
                           marker_color=[C["teal"] if r[2] >= target else C["signal"] for r in z]))
    return _layout(fig, title="On-time rate by zone", xaxis={"range": [0, 1], "tickformat": ".0%"}, margin={"l": 130, "t": 30})


def fig_fleet(P: dict[str, Any], city: str) -> go.Figure:
    f = P["fleet"][city]
    m = [f"{r['m']}x" for r in f]
    fig = go.Figure([go.Bar(name="Vans", x=m, y=[r["vans"] for r in f], marker_color=C["ink"]),
                     go.Bar(name="Tempos", x=m, y=[r["tempos"] for r in f], marker_color=C["teal"]),
                     go.Bar(name="2-wheelers", x=m, y=[r["tw"] for r in f], marker_color=C["marigold"]),
                     go.Scatter(name="Current fleet (25)", x=m, y=[25] * len(m), mode="lines", line={"dash": "dash", "color": C["signal"]})])
    return _layout(fig, barmode="stack", title="Recommended fleet and mix by demand level", yaxis_title="vehicles")


def fig_fleet_compliance(P: dict[str, Any], city: str) -> go.Figure:
    f = P["fleet"][city]
    m = [f"{r['m']}x" for r in f]
    fig = go.Figure([go.Scatter(name="Current fleet", x=m, y=[r["cur_comp"] for r in f], line={"color": C["slate"], "width": 3}),
                     go.Scatter(name="Recommended fleet", x=m, y=[r["rec_comp"] for r in f], line={"color": C["teal"], "width": 3}),
                     go.Scatter(name="95% target", x=m, y=[P["meta"]["compliance_target"]] * len(m), mode="lines",
                                line={"color": C["marigold"], "dash": "dot"})])
    return _layout(fig, title="On-time share by demand level", yaxis={"tickformat": ".0%", "range": [0.5, 1.01]})


def fig_heatmap(P: dict[str, Any], city: str, layout_name: str) -> go.Figure:
    g = P["warehouse"][city]["grid"][layout_name]
    fig = go.Figure(go.Heatmap(z=g, colorscale=[[0, "#FFFFFF"], [0.6, "#F2C14E"], [1, "#1F2A44"]], colorbar={"title": "picks"}))
    return _layout(fig, title=f"Pick intensity - {layout_name} layout", xaxis_title="aisle", yaxis_title="bay", height=520)


def fig_pph(P: dict[str, Any], city: str) -> go.Figure:
    pr = P["warehouse"][city]["perf"]
    vals = [pr[k]["picks_per_hour"] for k in ("baseline", "optimized", "perfect_information")]
    fig = go.Figure(go.Bar(x=["Current", "Optimized", "Perfect-information bound"], y=vals, text=[f"{v:.1f}" for v in vals],
                           marker_color=[C["slate"], C["teal"], C["marigold"]]))
    return _layout(fig, title="Pick lines per hour")


def fig_sim(P: dict[str, Any], city: str) -> go.Figure:
    s = P["simulation"][city]
    t = P["meta"]["target_cycle"]
    fig = go.Figure(go.Bar(x=[r["scenario"] for r in s], y=[r["pooled_p95"] for r in s], marker_color=SCEN_COLORS,
                           text=[f"{r['pooled_p95']:.0f} min ({'/'.join(map(str, r['staff']))})" for r in s]))
    fig.add_hline(y=t, line_dash="dash", line_color=C["ink"], annotation_text="2-hour target")
    return _layout(fig, title="P95 intake-to-dispatch minutes by roster", yaxis_title="minutes")


def npv(conf: dict[str, Any], wacc: float, growth: float, horizon: int) -> float:
    v = -conf["capex"]
    for t in range(1, horizon + 1):
        v += (conf["saving"] * (1 + growth) ** (t - 1) - conf["opex"] - conf["linehaul"]) / (1 + wacc) ** t
    return v


def fig_npv(P: dict[str, Any], city: str, p: int, wacc: float, growth: float) -> go.Figure:
    confs = [c for c in P["network"]["cities"][city]["configs"] if c["p"] > 0]
    vals = [npv(c, wacc, growth, P["meta"]["horizon"]) / 1e7 for c in confs]
    fig = go.Figure(go.Bar(x=[f"p={c['p']}" for c in confs], y=vals,
                           marker_color=[C["marigold"] if c["p"] == p else (C["teal"] if v >= 0 else C["slate"]) for c, v in zip(confs, vals)],
                           text=[f"₹{v:.2f} cr" for v in vals]))
    return _layout(fig, title=f"Five-year NPV at WACC {wacc:.1%}, growth {growth:.0%}", yaxis_title="₹ crore")


def fig_network_map(P: dict[str, Any], city: str, p: int) -> go.Figure:
    n = P["network"]["cities"][city]
    chosen = set(next(c for c in n["configs"] if c["p"] == p)["sites"])
    center = next(c["center"] for c in P["meta"]["cities"] if c["id"] == city)
    dep = P["routing"]["depots"][city]
    fig = go.Figure([go.Scattermap(lat=[s[2] for s in n["sites"]], lon=[s[3] for s in n["sites"]], mode="markers",
                                   marker={"size": [20 if s[0] in chosen else 11 for s in n["sites"]],
                                           "color": [C["teal"] if s[0] in chosen else C["marigold"] for s in n["sites"]]},
                                   hovertext=[f"{s[1]} - rent ₹{s[4]}/sqft/month" for s in n["sites"]], name="Candidate sites"),
                     go.Scattermap(lat=[d[3] for d in dep], lon=[d[4] for d in dep], mode="markers+text", text=[d[1] for d in dep],
                                   marker={"size": 14, "color": C["ink"]}, name="Existing depots", textposition="top right")])
    fig.update_layout(map={"style": "carto-positron", "center": {"lat": center[0], "lon": center[1]}, "zoom": 9.6},
                      margin={"l": 0, "r": 0, "t": 0, "b": 0}, height=520)
    return fig


# --------------------------------------------------------------------------- app
def kpi(value: str, label: str) -> html.Div:
    return html.Div([html.Div(value, className="v"), html.Div(label, className="l")], className="kpi")


def create_app(P: dict[str, Any] | None = None) -> Dash:
    P = P or load_payload()
    cities = P["meta"]["cities"]
    app = Dash(__name__, title="Output 6 - Logistics and Network Design", assets_folder=str(ROOT / "dashboard" / "static" / "assets"))
    graph = lambda i, h=360: dcc.Graph(id=i, style={"height": f"{h}px"}, config={"displayModeBar": False})  # noqa: E731
    app.layout = html.Div([
        html.Header([html.Div([html.H1("Last-mile network, five metros"),
                               html.P(f"Routing, fleet, warehouse, DC operations and MFC economics · routing day {P['meta']['day']}",
                                      className="lede")]),
                     dcc.RadioItems(id="city", value=cities[0]["id"], inline=True, className="city-radio",
                                    options=[{"label": f"{c['name']} ({', '.join(c['states'])})", "value": c["id"]} for c in cities])],
                    className="masthead"),
        html.Section(id="kpis", className="kpis"),
        dcc.Tabs(id="tab", value="overview", children=[
            dcc.Tab(label="Overview", value="overview", children=[html.Div([graph("ov_cost"), graph("ov_demand")], className="grid two")]),
            dcc.Tab(label="Route optimization map", value="routes", children=[
                dcc.RadioItems(id="plan", value="optimized", inline=True,
                               options=[{"label": "Optimized", "value": "optimized"}, {"label": "Manual baseline", "value": "baseline"}]),
                html.Div([graph("route_map", 520), graph("zone_bar", 520)], className="grid map")]),
            dcc.Tab(label="Fleet sizing", value="fleet", children=[html.Div([graph("fleet_mix"), graph("fleet_comp")], className="grid two")]),
            dcc.Tab(label="Warehouse heatmap", value="warehouse", children=[
                dcc.RadioItems(id="layout", value="optimized", inline=True,
                               options=[{"label": "Optimized", "value": "optimized"}, {"label": "Current", "value": "baseline"}]),
                html.Div([graph("heat", 520), graph("pph")], className="grid map")]),
            dcc.Tab(label="Simulation results", value="simulation", children=[graph("sim", 420)]),
            dcc.Tab(label="Network design & NPV", value="network", children=[
                html.Div([html.Label("New MFCs"), dcc.Slider(id="p", min=1, max=5, step=1, value=1),
                          html.Label("WACC %"), dcc.Slider(id="wacc", min=8, max=16, step=0.5, value=P["meta"]["wacc"] * 100,
                                                           marks={v: str(v) for v in range(8, 17, 2)}),
                          html.Label("Parcel growth %"), dcc.Slider(id="growth", min=0, max=40, step=1, value=P["meta"]["growth"] * 100,
                                                                    marks={v: str(v) for v in range(0, 41, 10)})], className="controls-col"),
                html.Div([graph("net_map", 520), graph("npv", 520)], className="grid map")]),
        ]),
    ], style={"padding": "0 24px 32px"})

    @app.callback(Output("kpis", "children"), Input("city", "value"))
    def _kpis(city: str) -> list[html.Div]:
        k = P["routing"]["kpis"][city]
        b, o = k["baseline"], k["optimized"]
        w = P["warehouse"][city]["comparison"]
        sim = next(s for s in P["simulation"][city] if s["scenario"] == "Optimized staffing")
        return [kpi(f"{1 - o['last_mile_cost'] / b['last_mile_cost']:.1%}", "Last-mile cost reduction"),
                kpi(f"{o['tw_compliance']:.1%}", "On-time in window"),
                kpi(f"{o['vehicles_used']} / {b['vehicles_used']}", "Vehicles, optimized / baseline"),
                kpi(f"{w['travel_reduction_pct']:.1f}%", "Less picker walking"),
                kpi(f"{sim['pooled_p95']:.0f} min", "DC P95 cycle, optimized roster")]

    app.callback(Output("ov_cost", "figure"), Input("city", "value"))(lambda c: fig_cost_by_city(P, c))
    app.callback(Output("ov_demand", "figure"), Input("city", "value"))(lambda c: fig_demand(P, c))
    app.callback(Output("route_map", "figure"), Input("city", "value"), Input("plan", "value"))(lambda c, p: fig_route_map(P, c, p))
    app.callback(Output("zone_bar", "figure"), Input("city", "value"), Input("plan", "value"))(lambda c, p: fig_zone_ontime(P, c, p))
    app.callback(Output("fleet_mix", "figure"), Input("city", "value"))(lambda c: fig_fleet(P, c))
    app.callback(Output("fleet_comp", "figure"), Input("city", "value"))(lambda c: fig_fleet_compliance(P, c))
    app.callback(Output("heat", "figure"), Input("city", "value"), Input("layout", "value"))(lambda c, lay: fig_heatmap(P, c, lay))
    app.callback(Output("pph", "figure"), Input("city", "value"))(lambda c: fig_pph(P, c))
    app.callback(Output("sim", "figure"), Input("city", "value"))(lambda c: fig_sim(P, c))
    app.callback(Output("net_map", "figure"), Input("city", "value"), Input("p", "value"))(lambda c, p: fig_network_map(P, c, int(p)))
    app.callback(Output("npv", "figure"), Input("city", "value"), Input("p", "value"), Input("wacc", "value"),
                 Input("growth", "value"))(lambda c, p, w, g: fig_npv(P, c, int(p), w / 100, g / 100))
    return app


if __name__ == "__main__":
    create_app().run(debug=False, host="127.0.0.1", port=8050)
