import json
import math
from collections import defaultdict
from pathlib import Path

import folium
import networkx as nx
import osmnx as ox
import pandas as pd
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

# -----------------------------
# Configuration
# -----------------------------
PLACE = "Berlin, Germany"
ASSUMED_BIKE_SPEED_KPH = 20.0
OUTPUT_DIR = Path("output")
OUTPUT_MAP = OUTPUT_DIR / "berlin_boulder_central_point.html"
OUTPUT_HALLS_CSV = OUTPUT_DIR / "berlin_boulder_halls_geocoded.csv"
OUTPUT_TRAVEL_TIMES_CSV = OUTPUT_DIR / "berlin_boulder_hall_travel_times_from_center.csv"
OUTPUT_SUMMARY_CSV = OUTPUT_DIR / "berlin_boulder_central_point_summary.csv"

CACHE_DIR = Path(".cache")
GEOCODE_CACHE_FILE = CACHE_DIR / "geocode_cache.json"
REVERSE_GEOCODE_CACHE_FILE = CACHE_DIR / "reverse_geocode_cache.json"
GRAPH_CACHE_FILE = CACHE_DIR / "berlin_bike.graphml"

# Current Berlin bouldering halls / studios.
HALLS = {
    "Berta Block": "Mühlenstraße 62, 13187 Berlin, Germany",
    "Boulderklub Kreuzberg": "Ohlauer Straße 38, 10999 Berlin, Germany",
    "Bouldergarten": "Thiemannstraße 1, 12059 Berlin, Germany",
    "Der Kegel": "Revaler Straße 99, 10245 Berlin, Germany",
    "Elektra": "Gustav-Meyer-Allee 25, 13355 Berlin, Germany",
    "Ostbloc": "Hauptstraße 13, 10317 Berlin, Germany",
    "Südbloc": "Großbeerenstraße 2-10, Haus 4, 12107 Berlin, Germany",
    "urban apes Basement Berlin": "Stresemannstraße 72, 10963 Berlin, Germany",
    "urban apes bright site Berlin": "Wilhelm-Kabus-Straße 40, 10829 Berlin, Germany",
    "urban apes Fhain Berlin": "Friedenstraße 91 B, 10249 Berlin, Germany",
    "urban apes Berlin Wedding": "Müllerstraße 46, 13349 Berlin, Germany",
}


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_json_cache(path: Path) -> dict:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json_cache(path: Path, data: dict) -> None:
    ensure_cache_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


def geocode_halls(halls: dict[str, str]) -> pd.DataFrame:
    """
    Geocode hall addresses with a local JSON cache.
    Cache key: exact address string.
    """
    ensure_cache_dir()
    cache = load_json_cache(GEOCODE_CACHE_FILE)

    geolocator = Nominatim(user_agent="berlin-boulder-central-point")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.0)

    rows = []
    cache_changed = False

    for name, address in halls.items():
        cached = cache.get(address)
        if cached is not None:
            lat = cached["lat"]
            lon = cached["lon"]
            resolved_address = cached.get("resolved_address")
            print(f"[cache] geocode: {name}")
        else:
            print(f"[live ] geocode: {name}")
            loc = geocode(address)
            if loc is None:
                raise RuntimeError(f"Could not geocode: {name} -> {address}")
            lat = loc.latitude
            lon = loc.longitude
            resolved_address = loc.address
            cache[address] = {
                "lat": lat,
                "lon": lon,
                "resolved_address": resolved_address,
            }
            cache_changed = True

        rows.append(
            {
                "name": name,
                "address": address,
                "lat": lat,
                "lon": lon,
                "resolved_address": resolved_address,
            }
        )

    if cache_changed:
        save_json_cache(GEOCODE_CACHE_FILE, cache)

    return pd.DataFrame(rows)


def reverse_geocode(lat: float, lon: float, precision: int = 6) -> str | None:
    """
    Reverse geocode with a local JSON cache.
    Cache key: rounded 'lat,lon' string, so tiny numeric differences don't miss cache.
    """
    ensure_cache_dir()
    cache = load_json_cache(REVERSE_GEOCODE_CACHE_FILE)

    key = f"{round(lat, precision)},{round(lon, precision)}"
    if key in cache:
        print("[cache] reverse geocode")
        return cache[key]

    print("[live ] reverse geocode")
    geolocator = Nominatim(user_agent="berlin-boulder-central-point-reverse")
    reverse = RateLimiter(geolocator.reverse, min_delay_seconds=1.0)
    loc = reverse((lat, lon), exactly_one=True)
    address = None if loc is None else loc.address

    cache[key] = address
    save_json_cache(REVERSE_GEOCODE_CACHE_FILE, cache)
    return address


def load_or_download_bike_graph(place: str) -> nx.MultiDiGraph:
    """
    Reuse a locally cached GraphML if present; otherwise download from OSM and save it.
    This usually saves much more time than geocoding cache alone.
    """
    ensure_cache_dir()
    if GRAPH_CACHE_FILE.exists():
        print(f"[cache] loading graph: {GRAPH_CACHE_FILE}")
        G = ox.load_graphml(GRAPH_CACHE_FILE)
    else:
        print(f"[live ] downloading graph for: {place}")
        G = ox.graph_from_place(place, network_type="bike", simplify=True, retain_all=False)
        ox.save_graphml(G, GRAPH_CACHE_FILE)
    return G


def add_constant_bike_times(G: nx.MultiDiGraph, bike_speed_kph: float) -> nx.MultiDiGraph:
    """Use a constant cycling speed to convert edge length to travel time."""
    meters_per_min = bike_speed_kph * 1000.0 / 60.0
    for _, _, _, data in G.edges(keys=True, data=True):
        length_m = float(data.get("length", 0.0))
        data["bike_time_min"] = length_m / meters_per_min if meters_per_min > 0 else math.inf
    return G


def find_best_node(
    G: nx.Graph | nx.MultiDiGraph,
    hall_nodes: dict[str, int],
    weight: str = "bike_time_min",
    directed: bool = True,
) -> tuple[int, float, pd.DataFrame]:
    """
    Find the network node with minimum average travel time to all halls.

    If directed=True:
      - G is assumed directed
      - shortest paths are run on G reversed, so distances correspond to
        candidate -> hall in the original graph

    If directed=False:
      - G is assumed undirected
    """
    search_graph = G.reverse(copy=False) if directed else G

    sum_time = defaultdict(float)
    reach_count = defaultdict(int)

    for hall_name, hall_node in hall_nodes.items():
        lengths = nx.single_source_dijkstra_path_length(search_graph, hall_node, weight=weight)
        for node, t in lengths.items():
            sum_time[node] += t
            reach_count[node] += 1

    required = len(hall_nodes)
    eligible_nodes = [n for n in G.nodes if reach_count[n] == required]

    if not eligible_nodes:
        raise RuntimeError("No node can reach all halls with the current graph settings.")

    best_node = min(eligible_nodes, key=lambda n: sum_time[n] / required)
    best_avg_min = sum_time[best_node] / required

    lengths_from_best = nx.single_source_dijkstra_path_length(G, best_node, weight=weight)
    hall_times = []
    for hall_name, hall_node in hall_nodes.items():
        hall_times.append(
            {
                "hall": hall_name,
                "travel_time_min": lengths_from_best[hall_node],
            }
        )

    hall_times = pd.DataFrame(hall_times).sort_values("travel_time_min").reset_index(drop=True)
    return best_node, best_avg_min, hall_times


def save_map(df_halls: pd.DataFrame, center_lat: float, center_lon: float, output_html: Path) -> None:
    m = folium.Map(location=[center_lat, center_lon], zoom_start=11, tiles="CartoDB positron")

    for _, row in df_halls.iterrows():
        folium.CircleMarker(
            location=[row["lat"], row["lon"]],
            radius=5,
            popup=f"{row['name']}<br>{row['address']}",
            fill=True,
        ).add_to(m)

    folium.Marker(
        location=[center_lat, center_lon],
        popup="Bike-time-optimal central point",
        tooltip="Bike-time-optimal central point",
    ).add_to(m)

    ensure_output_dir()
    m.save(str(output_html))


def main() -> None:
    ensure_output_dir()

    print("1) Geocoding boulder halls...")
    halls_df = geocode_halls(HALLS)

    print("2) Loading Berlin bike network...")
    G = load_or_download_bike_graph(PLACE)
    G = add_constant_bike_times(G, ASSUMED_BIKE_SPEED_KPH)

    print("3) Snapping halls to nearest bike-network nodes...")
    hall_node_ids = ox.distance.nearest_nodes(
        G,
        X=halls_df["lon"].to_list(),
        Y=halls_df["lat"].to_list(),
    )
    halls_df["node"] = hall_node_ids
    hall_nodes = dict(zip(halls_df["name"], halls_df["node"]))

    print("4) Solving for the minimum-average-time node...")
    try:
        best_node, best_avg_min, hall_times = find_best_node(
            G,
            hall_nodes,
            weight="bike_time_min",
            directed=True,
        )
        graph_used = G
        mode = "directed bike graph"
    except RuntimeError:
        UG = G.to_undirected(as_view=False)
        best_node, best_avg_min, hall_times = find_best_node(
            UG,
            hall_nodes,
            weight="bike_time_min",
            directed=False,
        )
        graph_used = UG
        mode = "undirected fallback"

    center_lat = graph_used.nodes[best_node]["y"]
    center_lon = graph_used.nodes[best_node]["x"]
    center_address = reverse_geocode(center_lat, center_lon)

    print("\n=== Result ===")
    print(f"Mode: {mode}")
    print(f"Best node ID: {best_node}")
    print(f"Coordinates: {center_lat:.6f}, {center_lon:.6f}")
    print(f"Average bike travel time to all halls: {best_avg_min:.2f} minutes")
    if center_address:
        print(f"Approx. address: {center_address}")

    print("\nPer-hall travel times from the best point:")
    print(hall_times.to_string(index=False))

    save_map(halls_df, center_lat, center_lon, OUTPUT_MAP)
    halls_df.to_csv(OUTPUT_HALLS_CSV, index=False)
    hall_times.to_csv(OUTPUT_TRAVEL_TIMES_CSV, index=False)

    summary = pd.DataFrame(
        [
            {
                "mode": mode,
                "best_node": best_node,
                "lat": center_lat,
                "lon": center_lon,
                "avg_bike_time_min": best_avg_min,
                "approx_address": center_address,
            }
        ]
    )
    summary.to_csv(OUTPUT_SUMMARY_CSV, index=False)

    print(f"\nSaved map to: {OUTPUT_MAP}")
    print(f"Saved geocoded halls to: {OUTPUT_HALLS_CSV}")
    print(f"Saved per-hall travel times to: {OUTPUT_TRAVEL_TIMES_CSV}")
    print(f"Saved summary to: {OUTPUT_SUMMARY_CSV}")
    print(f"Cache directory: {CACHE_DIR.resolve()}")


if __name__ == "__main__":
    main()
