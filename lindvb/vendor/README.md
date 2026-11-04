# Third-party code

| File | Project | Version | Licence |
|---|---|---|---|
| `dash.mediaplayer.min.js` | [dash.js](https://github.com/Dash-Industry-Forum/dash.js) (DASH Industry Forum), MPEG-DASH player on Media Source Extensions | 4.7.4, from the npm package `dashjs` | BSD 3-Clause ([dash.js-LICENSE.md](dash.js-LICENSE.md)); bundled parts in `dash.mediaplayer.min.js.LICENSE.txt` |

LinDVB's HbbTV terminal (`lindvb/hbbtv.js`) loads dash.js into every HbbTV
application page, without a global name, to play MPEG-DASH in `<video>`
elements and A/V control objects.
