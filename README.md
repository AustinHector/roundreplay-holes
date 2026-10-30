# roundreplay-holes

Hole maps for RoundReplay: one gzip file per golf course, built from OpenStreetMap every Sunday.

- `build.py` downloads Geofabrik's regional extracts one at a time, resuming a download that drops from the bytes already on disk, keeps the golf data near each course, and writes `v1/<course id>.json.gz`: the course id with `:` replaced by `-`, for example `v1/osm-way-27130579.json.gz`.
- `.github/workflows/build.yml` runs the tests and the build on Sundays at 06:00 UTC, or by hand, then deploys the files to GitHub Pages. A plan job picks the regions and deals them to 8 shards by size; 8 shard jobs run at once, each downloading and cutting its own regions; a merge job joins what they return, assigns and trims the files and runs the guards. It deploys only when four publish guards pass; otherwise the run fails and the last deployment stays live.
- `build.json` records the last published build: file counts, sizes, the OpenStreetMap date, course-list coverage and stage timings. Each successful build commits it.
- `python3 -m unittest` runs the tests. They need `osmium-tool` and no network.

## The file format

Each file is JSON in the shape of an Overpass `out geom` response, so the app's parser reads it unchanged. The top-level keys are:

- `version`: 1.
- `generated`: the build time, ISO 8601.
- `osm_base`: the timestamp of the OpenStreetMap data used.
- `copyright`: "© OpenStreetMap contributors".
- `license`: "ODbL 1.0".
- `elements`: the course outline (`leisure=golf_course`), every `golf=hole`, `green`, `tee`, `fairway`, `bunker`, `water_hazard` and `lateral_water_hazard`, and `natural=water`. Each element keeps only the tags `golf`, `natural`, `leisure`, `name`, `ref`, `par` and `handicap`.

A file exists only for a course with at least one `golf=hole` way. A feature belongs to a course when any of its vertices lies inside the course outline. Bunker and water rings are clipped to the course box, the bounding box of the outline and the hole lines padded by 250 m; each clipped ring is written as its own closed outer member with coordinates rounded to 7 decimals. Inner members, rings clipped to nothing and point hazards are dropped. The files are compressed with gzip and have no file name or time in the header.

## Publish guards

The build deploys only when all four hold:

1. At least 15,000 files, and no more than 5% fewer than the last `build.json`.
2. Every file inflates and holds an `elements` array with at least one `golf=hole`.
3. At most 1% of the published course list's ids have no outline in the extracts.
4. No file is over 1 MB compressed.

## Failure notices

GitHub emails the person who created the workflow when a scheduled run fails, once this is on. On github.com: profile picture, Settings, Notifications, scroll to System, Actions, then choose email and "Only notify for failed workflows".

## License

Data © OpenStreetMap contributors, available under the Open Database License (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/

The files are a derived database. Anyone who makes them available must keep them under the ODbL and credit OpenStreetMap contributors.
