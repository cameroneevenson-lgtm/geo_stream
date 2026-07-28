# Disaster recovery

## Prerequisites and location

- Clone to `C:\Tools\geo_stream` and rebuild the shared Python 3.12 environment.
- Restore normal internet access to CHS, ECCC/MSC Datamart, GeoMet, OpenStreetMap, and package sources.
- Install cloudflared only if temporary external access is still required.

## Install

```powershell
Set-Location C:\Tools\geo_stream
.\setup_env.bat
```

For a standalone clone outside `C:\Tools`, follow the project README and create a project-local environment.

## State and exclusions

Geo Stream is intentionally stateless for disaster recovery. Forecast downloads, recent CHS bundles, ROI
GeoJSON, metadata, rendered maps, caches, synthetic test data, tunnel state, and logs are recreated or fetched
again. There is no repository-specific entry in the encrypted live-state bundle.

## Restore and verify

1. Run `C:\Tools\.venv\Scripts\python.exe -m pytest -q`.
2. Start `run_local.bat` and verify a known CHS station can be fetched.
3. Draw a small region and verify ECCC archive/GeoMet discovery and clear empty-data labeling.
4. Exercise synthetic mode to confirm all risk colours without confusing it for official data.
5. If needed, start `run_tunnel.bat` only after loopback operation succeeds; treat quick tunnels as temporary.
