import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import cast
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

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
ENABLE_ELEVATION_ADJUSTMENT = True
ELEVATION_API_URL_TEMPLATE = "https://api.opentopodata.org/v1/eudem25m?locations={locations}"
ELEVATION_API_BATCH_SIZE = 100
ELEVATION_API_PAUSE_SECONDS = 1.0
ELEVATION_GRID_STEP_DEGREES = 0.01
MAX_ABS_GRADE = 0.12
MIN_BIKE_SPEED_KPH = 8.0
MAX_BIKE_SPEED_KPH = 40.0
OUTPUT_DIR = Path("output")
OUTPUT_MAP = OUTPUT_DIR / "berlin_boulder_central_point.html"
OUTPUT_HALLS_CSV = OUTPUT_DIR / "berlin_boulder_halls_geocoded.csv"
OUTPUT_TRAVEL_TIMES_CSV = OUTPUT_DIR / "berlin_boulder_hall_travel_times_from_center.csv"
OUTPUT_SUMMARY_CSV = OUTPUT_DIR / "berlin_boulder_central_point_summary.csv"

CACHE_DIR = Path(".cache")
GEOCODE_CACHE_FILE = CACHE_DIR / "geocode_cache.json"
REVERSE_GEOCODE_CACHE_FILE = CACHE_DIR / "reverse_geocode_cache.json"
CACHE_VERSION = "v1"

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


def slugify_cache_token(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "default"


def get_graph_cache_file(place: str) -> Path:
    return CACHE_DIR / f"{slugify_cache_token(place)}_bike.graphml"


def get_elevated_graph_cache_file(place: str) -> Path:
    config_token = "_".join(
        [
            slugify_cache_token(place),
            CACHE_VERSION,
            f"grid-{ELEVATION_GRID_STEP_DEGREES}",
            f"batch-{ELEVATION_API_BATCH_SIZE}",
            f"max-grade-{MAX_ABS_GRADE}",
            f"min-speed-{MIN_BIKE_SPEED_KPH}",
            f"max-speed-{MAX_BIKE_SPEED_KPH}",
        ]
    )
    return CACHE_DIR / f"{config_token}_bike_elevated.graphml"


def load_json_cache(path: Path) -> dict:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn ] ignoring unreadable cache file {path}: {exc}")
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
    graph_cache_file = get_graph_cache_file(place)
    if graph_cache_file.exists():
        print(f"[cache] loading graph: {graph_cache_file}")
        G = ox.load_graphml(graph_cache_file)
    else:
        print(f"[live ] downloading graph for: {place}")
        G = ox.graph_from_place(place, network_type="bike", simplify=True, retain_all=False)
        G.graph["cache_place"] = place
        ox.save_graphml(G, graph_cache_file)
    return G


def fetch_elevation_batch(points: list[tuple[float, float]]) -> list[float]:
    locations = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    url = ELEVATION_API_URL_TEMPLATE.format(locations=quote(locations, safe=","))
    request = Request(url, headers={"User-Agent": "berlin-boulder-central-point"})

    with urlopen(request, timeout=180) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if payload.get("status") not in {None, "OK"}:
        raise RuntimeError(f"Elevation API returned status {payload.get('status')!r}")

    results = payload.get("results", [])
    if len(results) != len(points):
        raise RuntimeError(
            f"Elevation API returned {len(results)} results for {len(points)} requested points"
        )

    elevations = []
    for result in results:
        elevation = result.get("elevation")
        if elevation is None:
            raise RuntimeError(f"Elevation missing in API response: {result}")
        elevations.append(float(elevation))

    return elevations


def add_approximate_node_elevations(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """Approximate node elevations by sampling a coarse grid over the graph bounds."""
    latitudes = [float(data["y"]) for _, data in G.nodes(data=True)]
    longitudes = [float(data["x"]) for _, data in G.nodes(data=True)]

    south = min(latitudes)
    north = max(latitudes)
    west = min(longitudes)
    east = max(longitudes)

    lat_count = max(2, math.ceil((north - south) / ELEVATION_GRID_STEP_DEGREES) + 1)
    lon_count = max(2, math.ceil((east - west) / ELEVATION_GRID_STEP_DEGREES) + 1)

    grid_points = []
    for lat_index in range(lat_count):
        lat = min(north, south + lat_index * ELEVATION_GRID_STEP_DEGREES)
        for lon_index in range(lon_count):
            lon = min(east, west + lon_index * ELEVATION_GRID_STEP_DEGREES)
            grid_points.append((lat_index, lon_index, lat, lon))

    print(
        f"[live ] sampling elevation grid: {lat_count} x {lon_count} = {len(grid_points)} points"
    )

    sampled_elevations: dict[tuple[int, int], float] = {}
    for start in range(0, len(grid_points), ELEVATION_API_BATCH_SIZE):
        batch = grid_points[start : start + ELEVATION_API_BATCH_SIZE]
        coordinates = [(lat, lon) for _, _, lat, lon in batch]
        elevations = fetch_elevation_batch(coordinates)
        for (lat_index, lon_index, _, _), elevation in zip(batch, elevations, strict=True):
            sampled_elevations[(lat_index, lon_index)] = elevation
        if start + ELEVATION_API_BATCH_SIZE < len(grid_points):
            time.sleep(ELEVATION_API_PAUSE_SECONDS)

    for node_id, data in G.nodes(data=True):
        lat_index = int(round((float(data["y"]) - south) / ELEVATION_GRID_STEP_DEGREES))
        lon_index = int(round((float(data["x"]) - west) / ELEVATION_GRID_STEP_DEGREES))
        lat_index = max(0, min(lat_count - 1, lat_index))
        lon_index = max(0, min(lon_count - 1, lon_index))
        data["elevation"] = sampled_elevations[(lat_index, lon_index)]

    G.graph["elevation_model"] = "grid-approximation"
    G.graph["elevation_grid_step_degrees"] = ELEVATION_GRID_STEP_DEGREES
    return G


def add_edge_grades_from_node_elevations(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    for u, v, _, data in G.edges(keys=True, data=True):
        length_m = float(data.get("length", 0.0))
        if length_m <= 0:
            grade = 0.0
        else:
            rise_m = float(G.nodes[v].get("elevation", 0.0)) - float(G.nodes[u].get("elevation", 0.0))
            grade = rise_m / length_m
        data["grade"] = grade
        data["grade_abs"] = abs(grade)
    return G


def graph_has_node_elevations(G: nx.MultiDiGraph) -> bool:
    return all("elevation" in data for _, data in G.nodes(data=True))


def graph_has_edge_grades(G: nx.MultiDiGraph) -> bool:
    return all("grade" in data for _, _, _, data in G.edges(keys=True, data=True))


def graph_matches_elevation_config(G: nx.MultiDiGraph, place: str) -> bool:
    return (
        G.graph.get("cache_place") == place
        and str(G.graph.get("elevation_model")) == "grid-approximation"
        and float(G.graph.get("elevation_grid_step_degrees", -1.0)) == ELEVATION_GRID_STEP_DEGREES
    )


class NoReachableCommonNodeError(RuntimeError):
    pass


def load_or_prepare_bike_graph(place: str) -> tuple[nx.MultiDiGraph, str]:
    """
    Load the bike graph from cache when possible and enrich it with elevations
    and grades when that optional data is available.
    """
    ensure_cache_dir()
    elevated_graph_cache_file = get_elevated_graph_cache_file(place)

    if ENABLE_ELEVATION_ADJUSTMENT and elevated_graph_cache_file.exists():
        print(f"[cache] loading elevated graph: {elevated_graph_cache_file}")
        elevated_graph = ox.load_graphml(elevated_graph_cache_file)
        if (
            graph_has_node_elevations(elevated_graph)
            and graph_has_edge_grades(elevated_graph)
            and graph_matches_elevation_config(elevated_graph, place)
        ):
            return elevated_graph, "grade-adjusted heuristic"
        print("[warn ] cached elevated graph is incomplete or stale, rebuilding it")

    G = load_or_download_bike_graph(place)
    G.graph["cache_place"] = place

    if not ENABLE_ELEVATION_ADJUSTMENT:
        return G, "flat speed"

    try:
        if not graph_has_node_elevations(G):
            G = add_approximate_node_elevations(G)

        if not graph_has_edge_grades(G):
            print("[calc ] computing approximate edge grades...")
            G = add_edge_grades_from_node_elevations(G)

        ox.save_graphml(G, elevated_graph_cache_file)
        return G, "grade-adjusted heuristic"
    except Exception as exc:
        print(f"[warn ] elevation enrichment unavailable, using flat-speed fallback: {exc}")
        return G, "flat speed"


def grade_adjusted_speed_kph(grade: float, flat_speed_kph: float) -> float:
    """
    Convert road grade to a heuristic cycling speed.

    Uphill segments slow the rider down exponentially. Downhill segments only
    provide a modest boost, then the result is capped to avoid unrealistic
    routing behavior on steeper grades.
    """
    clamped_grade = max(-MAX_ABS_GRADE, min(MAX_ABS_GRADE, grade))

    if clamped_grade >= 0:
        speed_kph = flat_speed_kph * math.exp(-4.5 * clamped_grade)
    else:
        speed_kph = flat_speed_kph * (1.0 + 1.8 * abs(clamped_grade))

    return max(MIN_BIKE_SPEED_KPH, min(MAX_BIKE_SPEED_KPH, speed_kph))


def add_bike_times(G: nx.MultiDiGraph, bike_speed_kph: float) -> nx.MultiDiGraph:
    """Convert edge length and grade into travel time using a capped speed heuristic."""
    for _, _, _, data in G.edges(keys=True, data=True):
        length_m = float(data.get("length", 0.0))
        grade = float(data.get("grade", 0.0))
        adjusted_speed_kph = grade_adjusted_speed_kph(grade, bike_speed_kph)
        data["bike_speed_kph"] = adjusted_speed_kph
        meters_per_min = adjusted_speed_kph * 1000.0 / 60.0
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
    if directed:
        search_graph = cast(nx.MultiDiGraph, G).reverse(copy=False)
    else:
        search_graph = G

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
        raise NoReachableCommonNodeError(
            "No node can reach all halls with the current graph settings."
        )

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
    G, speed_model = load_or_prepare_bike_graph(PLACE)
    G = add_bike_times(G, ASSUMED_BIKE_SPEED_KPH)

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
    except NoReachableCommonNodeError:
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
    print(f"Speed model: {speed_model}")
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
                "speed_model": speed_model,
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
    print(f"Bike graph cache: {get_graph_cache_file(PLACE)}")
    if ENABLE_ELEVATION_ADJUSTMENT:
        print(f"Elevated graph cache: {get_elevated_graph_cache_file(PLACE)}")


if __name__ == "__main__":
    main()
