# mijnrit-data

Nightly GVB network + timetable file for the **Mijn Rit** iPhone app (https://mijnritapp.nl).

A GitHub Action downloads the open GTFS feed from OVapi (gtfs.ovapi.nl), keeps only GVB
(trams, metros, buses, ferries in Amsterdam) and publishes two files on GitHub Pages:

- `gvb.json` – routes, stops, shapes, stop patterns and 14 days of trips
- `version.json` – when it was generated, which dates it covers, size and SHA-256

Source data: OVapi / GVB open data.
