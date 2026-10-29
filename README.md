# roundreplay-holes

Hole maps for RoundReplay: one gzip file per golf course, built from OpenStreetMap.

- `build.py` downloads Geofabrik's regional extracts one at a time, keeps the golf data near each course, and writes `v1/<course id>.json.gz` (the course id with `:` replaced by `-`, for example `v1/osm-way-27130579.json.gz`).
- `.github/workflows/spike.yml` runs the build by hand and deploys the files to GitHub Pages. It is a spike: it has no schedule.

Each file is JSON in the shape of an Overpass `out geom` response, with the top-level keys `version`, `generated`, `osm_base`, `copyright`, `license` and `elements`.

## License

Data © OpenStreetMap contributors, available under the Open Database License (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/

The files are a derived database. Anyone who makes them available must keep them under the ODbL and credit OpenStreetMap contributors.
