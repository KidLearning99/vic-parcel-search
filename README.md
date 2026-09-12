# Parcel — Victorian property search

Type a Victorian address, get its land, title and planning facts. Static site,
no backend, no API key. Deploys to GitHub Pages.

```
index.html                         the whole app
build-medians.py                   generates data/suburb-medians.json
data/suburb-medians.json           suburb medians (786 suburbs, ~429 KB)
.github/workflows/update-medians.yml
```

## Deploy

Push to a repo, then Settings → Pages → deploy from `main`, root. That's it —
`index.html` queries Vicmap directly from the browser.

## What it shows

For an address: lot and plan, Standard Parcel Identifier, true land area,
council, ward, urban growth boundary status, ABS mesh block, planning zone,
every overlay affecting the land, bushfire-prone status, named schools /
transport / health / recreation within 1.6 km, and suburb median prices.

The hero is the actual lot boundary drawn from the survey coordinates, filled
with its Victoria Planning Provisions zone colour so it reads against VicPlan.

## What it does not show, and cannot

**No per-address sale price or valuation.** No free dataset has one. CoreLogic
and PropTrack are enterprise contracts; Domain gates sale history behind a paid
tier. Suburb medians from the Valuer-General are the finest free grain available,
and they lag — the December 2025 quarter was released in June 2026.

## Data sources

All Vicmap layers are ArcGIS Online feature services sending
`access-control-allow-origin: *`, which is why the browser can call them with no
proxy:

| Layer | Used for |
|---|---|
| Vicmap Address | address search, mesh block, postcode |
| Vicmap Parcel | lot/plan, SPI, boundary geometry |
| Vicmap Planning | zone, overlays, UGB, bushfire-prone areas |
| Vicmap Admin | council, ward, locality |
| Vicmap Features of Interest | nearby named features |
| Valuer-General VPSR (via DataVic CKAN) | suburb medians |

Vicmap data © State of Victoria (DTP), CC BY 4.0.

## Gotchas worth keeping

Things that cost real debugging time:

- **`orderByFields` breaks the Vicmap Address layer.** It returns an empty body
  rather than an error. Sort client-side.
- **Never send unanchored `LIKE '%10%'`.** Across 3.5M addresses the server
  gives up and returns nothing. The address is parsed into `house_number_1`,
  `road_name` and `locality_name` first; that's 335–800 ms instead of a timeout.
- **`Shape__Area` is Web Mercator** and overstates area by ~60% at Melbourne's
  latitude. Area is computed by shoelace on EPSG:3111 (VicGrid94) instead.
- **land.vic.gov.au is behind Cloudflare.** A User-Agent alone isn't enough —
  you need the `__cf_bm` cookie from a warm-up request *and* curl's TLS
  fingerprint. Python's urllib and requests both get 403 holding a valid cookie,
  which is why the builder shells out to curl.
- **DataVic CKAN carries dead links.** The December 2025 unit and vacant-land
  resources both 404. The builder walks back to the newest quarter that actually
  downloads, so houses can be a quarter ahead of units and land.
- **The house and unit workbooks have different header layouts** — the month
  range sits on row 1 for houses and row 0 for units, formatted `Oct-Dec` versus
  `Jul - Sep`. The parser scans the top rows rather than assuming positions.
- **A `^` beside a median** means the Valuer-General considers it unreliable
  (too few sales). It's preserved as the `unreliable` flag.

## Refreshing medians

```bash
pip install xlrd
python3 build-medians.py -o data/suburb-medians.json
```

The workflow runs this monthly and commits only when the data changes. The
script exits non-zero if it parses fewer than 300 suburbs, so a silent upstream
format change fails the build instead of publishing an empty file.
