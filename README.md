# boulder-center

Compute a bike-time-optimal meeting point for a set of Berlin bouldering gyms.

The script geocodes a hardcoded list of bouldering halls, downloads or reuses the Berlin bike network from OpenStreetMap via OSMnx, estimates cycling travel times with a constant bike speed, and finds the network node with the lowest average travel time to every hall.

It then writes a small set of artifacts:

- An interactive HTML map with the gyms and the computed center point
- A CSV of geocoded gym locations
- A CSV of travel times from the computed center to each gym
- A one-row CSV summary of the chosen point

## What It Does

The current workflow is fixed in code:

- Place: Berlin, Germany
- Travel mode: bike network from OpenStreetMap
- Assumed cycling speed: 20 km/h
- Gym list: hardcoded in `main.py`

High-level flow:

1. Geocode each gym address with Nominatim and cache the result locally.
2. Load a cached Berlin bike graph or download it from OpenStreetMap.
3. Snap each gym to its nearest bike-network node.
4. Compute the node with minimum average shortest-path travel time to all gyms.
5. Reverse geocode the selected node for a readable approximate address.
6. Save the map and CSV outputs.

## Requirements

- Python 3.13+
- Internet access on the first run for:
	- Nominatim geocoding
	- OpenStreetMap graph download

The project already includes a `pyproject.toml` and `uv.lock`, so `uv` is the simplest way to install dependencies.

## Setup

### Option 1: `uv` (recommended)

```bash
uv sync
```

Run the script:

```bash
uv run python main.py
```

### Option 2: `venv` + `pip`

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
python main.py
```

## Output Files

The script writes results to `output/`:

- `output/berlin_boulder_central_point.html`
- `output/berlin_boulder_halls_geocoded.csv`
- `output/berlin_boulder_hall_travel_times_from_center.csv`
- `output/berlin_boulder_central_point_summary.csv`

It also caches downloaded and geocoded data in `.cache/`:

- `.cache/geocode_cache.json`
- `.cache/reverse_geocode_cache.json`
- `.cache/berlin_bike.graphml`

The first run is slower. Later runs are much faster if the cache is still present.

## Example Console Output

```text
1) Geocoding boulder halls...
2) Loading Berlin bike network...
3) Snapping halls to nearest bike-network nodes...
4) Solving for the minimum-average-time node...

=== Result ===
Mode: directed bike graph
Best node ID: ...
Coordinates: ..., ...
Average bike travel time to all halls: ... minutes
Approx. address: ...
```

## Notes And Limitations

- The gym list is not loaded from a file or CLI argument yet; it is defined directly in `main.py`.
- The place, output filenames, and assumed bike speed are also hardcoded.
- Travel time is estimated from edge length and a constant cycling speed. It does not model elevation, traffic signals, surface quality, or real routing speeds.
- Reverse geocoding returns an approximate nearby address, not a guaranteed destination or venue.
- If the directed bike graph cannot reach all halls, the script falls back to an undirected graph.

## Project Structure

```text
.
├── main.py
├── pyproject.toml
├── uv.lock
├── .cache/          # generated
└── output/          # generated
```

## Customization

If you want to adapt this project, the main places to edit are near the top of `main.py`:

- `PLACE`
- `ASSUMED_BIKE_SPEED_KPH`
- `HALLS`
- output file paths

## License

MIT
