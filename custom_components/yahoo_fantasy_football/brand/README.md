# Integration branding

The existing purple football artwork is shipped with the integration:

| Asset | Size |
| --- | --- |
| `icon.png` | 256 × 256 |
| `icon@2x.png` | 512 × 512 |
| `logo.png` | 256 × 256 |
| `logo@2x.png` | 512 × 512 |

The original icon and logo design is preserved. The high-resolution logo uses
the existing high-resolution icon because this integration's logo is square.

Home Assistant 2026.3+ can serve these bundled assets through its brands API,
for example `/api/brands/integration/yahoo_fantasy_football/icon.png`. Earlier
HA versions may not display local brand assets; this does not affect scoring.

HACS's catalog icon handling depends on the HACS version and its brand service.
Some versions do not use HA's local brands endpoint even when the integration
ships the correct assets. Replacing `icon.png` or adding an unsupported `icon`
field to `hacs.json` does not fix that catalog issue. No HACS-internal cache or
central brand-service modification is included here.

References:

* https://contributors.home-assistant.io/core/integrations/brands/
* https://github.com/hacs/integration/issues/5171
